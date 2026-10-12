package com.inkomoko.fineract.glsync.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.google.gson.JsonParser;
import com.inkomoko.fineract.glsync.domain.GlAccountExternalRef;
import com.inkomoko.fineract.glsync.domain.GlAccountExternalRefRepository;
import com.inkomoko.fineract.glsync.domain.PayloadHasher;
import com.inkomoko.fineract.glsync.exception.GlAccountSyncConflictException;
import java.util.Optional;
import org.apache.fineract.accounting.glaccount.domain.GLAccount;
import org.apache.fineract.accounting.glaccount.domain.GLAccountRepository;
import org.apache.fineract.accounting.glaccount.service.GLAccountWritePlatformService;
import org.apache.fineract.infrastructure.core.api.JsonCommand;
import org.apache.fineract.infrastructure.core.data.CommandProcessingResult;
import org.apache.fineract.infrastructure.core.data.CommandProcessingResultBuilder;
import org.apache.fineract.infrastructure.core.serialization.FromJsonHelper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

@ExtendWith(MockitoExtension.class)
class GlAccountSyncServiceTest {

    private static final String BODY = """
            {"name":"Loan Receivable","glCode":"1200","type":1,"usage":1,"manualEntriesAllowed":false}""";

    @Mock
    private AdvisoryLocks locks;
    @Mock
    private GlAccountExternalRefRepository refs;
    @Mock
    private GLAccountRepository glAccounts;
    @Mock
    private GLAccountWritePlatformService coreGlAccountService;

    private GlAccountSyncService service;

    @BeforeEach
    void setUp() {
        service = new GlAccountSyncService(locks, refs, glAccounts, coreGlAccountService);
    }

    @Test
    @DisplayName("new reference, free code: creates through core, stores the reference with the payload hash")
    void createsNewAccount() {
        JsonCommand command = command(BODY);
        when(refs.findByExternalReference("GL-1200")).thenReturn(Optional.empty());
        when(glAccounts.findOneByGlCode("1200")).thenReturn(Optional.empty());
        when(coreGlAccountService.createGLAccount(command)).thenReturn(new CommandProcessingResultBuilder().withEntityId(42L).build());

        CommandProcessingResult result = service.sync("GL-1200", command);

        assertThat(result.getResourceId()).isEqualTo(42L);
        assertThat(result.getChanges()).containsEntry("outcome", "CREATED").containsEntry("erpReference", "GL-1200").containsEntry("glCode",
                "1200");
        ArgumentCaptor<GlAccountExternalRef> saved = ArgumentCaptor.forClass(GlAccountExternalRef.class);
        verify(refs).saveAndFlush(saved.capture());
        assertThat(saved.getValue().getGlAccountId()).isEqualTo(42L);
        assertThat(saved.getValue().getExternalReference()).isEqualTo("GL-1200");
        assertThat(saved.getValue().getPayloadHash()).isEqualTo(PayloadHasher.sha256(BODY));
        verify(locks).lockInOrder("glsync:ref:GL-1200", "glsync:code:1200");
    }

    @Test
    @DisplayName("same reference, same payload: returns the existing account and writes nothing")
    void returnsExistingForSamePayload() {
        when(refs.findByExternalReference("GL-1200")).thenReturn(Optional.of(new GlAccountExternalRef(42L, "GL-1200", PayloadHasher.sha256(BODY))));

        CommandProcessingResult result = service.sync("GL-1200", command(BODY));

        assertThat(result.getResourceId()).isEqualTo(42L);
        assertThat(result.getChanges()).containsEntry("outcome", "UNCHANGED");
        verify(coreGlAccountService, never()).createGLAccount(any());
        verify(refs, never()).saveAndFlush(any());
    }

    @Test
    @DisplayName("same reference, different payload: 409 conflict and nothing written")
    void rejectsDifferentPayload() {
        when(refs.findByExternalReference("GL-1200")).thenReturn(Optional.of(new GlAccountExternalRef(42L, "GL-1200", PayloadHasher.sha256(BODY))));

        assertThatThrownBy(() -> service.sync("GL-1200", command(BODY.replace("Loan Receivable", "Loans Receivable"))))
                .isInstanceOf(GlAccountSyncConflictException.class)
                .hasFieldOrPropertyWithValue("globalisationMessageCode", "error.msg.glaccount.sync.payload.conflict");
        verify(coreGlAccountService, never()).createGLAccount(any());
        verify(refs, never()).saveAndFlush(any());
    }

    @Test
    @DisplayName("new reference, GL code already used: 409 conflict and nothing written")
    void rejectsTakenCode() {
        when(refs.findByExternalReference("GL-1200-B")).thenReturn(Optional.empty());
        when(glAccounts.findOneByGlCode("1200")).thenReturn(Optional.of(mock(GLAccount.class)));

        assertThatThrownBy(() -> service.sync("GL-1200-B", command(BODY))).isInstanceOf(GlAccountSyncConflictException.class)
                .hasFieldOrPropertyWithValue("globalisationMessageCode", "error.msg.glaccount.sync.code.conflict");
        verify(coreGlAccountService, never()).createGLAccount(any());
        verify(refs, never()).saveAndFlush(any());
    }

    @Test
    @DisplayName("locks and looks up the trimmed GL code, as core stores it trimmed")
    void trimsGlCode() {
        JsonCommand command = command(BODY.replace("\"1200\"", "\" 1200 \""));
        when(refs.findByExternalReference("GL-1200")).thenReturn(Optional.empty());
        when(glAccounts.findOneByGlCode("1200")).thenReturn(Optional.empty());
        when(coreGlAccountService.createGLAccount(command)).thenReturn(new CommandProcessingResultBuilder().withEntityId(42L).build());

        CommandProcessingResult result = service.sync("GL-1200", command);

        verify(locks).lockInOrder("glsync:ref:GL-1200", "glsync:code:1200");
        assertThat(result.getChanges()).containsEntry("glCode", "1200");
    }

    @Test
    @DisplayName("takes the locks before reading anything")
    void locksBeforeReading() {
        when(refs.findByExternalReference("GL-1200")).thenReturn(Optional.of(new GlAccountExternalRef(42L, "GL-1200", PayloadHasher.sha256(BODY))));

        service.sync("GL-1200", command(BODY));

        var order = org.mockito.Mockito.inOrder(locks, refs);
        order.verify(locks).lockInOrder("glsync:ref:GL-1200", "glsync:code:1200");
        order.verify(refs).findByExternalReference("GL-1200");
    }

    @Test
    @DisplayName("child account: resolves the parent's ERP reference and hands core its parentId, not the ERP reference")
    void createsChildUnderHeaderParent() {
        String body = BODY.replace("}", ",\"parentAccount\":\"GL-1000\"}");
        JsonCommand command = command(body);
        GLAccount parent = headerParent(7L, 1, true);
        when(refs.findByExternalReference("GL-1200")).thenReturn(Optional.empty());
        when(glAccounts.findOneByGlCode("1200")).thenReturn(Optional.empty());
        when(refs.findByExternalReference("GL-1000")).thenReturn(Optional.of(new GlAccountExternalRef(7L, "GL-1000", "hash")));
        when(glAccounts.findById(7L)).thenReturn(Optional.of(parent));
        when(coreGlAccountService.createGLAccount(any())).thenReturn(new CommandProcessingResultBuilder().withEntityId(42L).build());

        CommandProcessingResult result = service.sync("GL-1200", command);

        ArgumentCaptor<JsonCommand> sentToCore = ArgumentCaptor.forClass(JsonCommand.class);
        verify(coreGlAccountService).createGLAccount(sentToCore.capture());
        var coreBody = JsonParser.parseString(sentToCore.getValue().json()).getAsJsonObject();
        assertThat(coreBody.get("parentId").getAsLong()).isEqualTo(7L);
        assertThat(coreBody.has("parentAccount")).isFalse();
        assertThat(result.getChanges()).containsEntry("parentAccount", "GL-1000").containsEntry("outcome", "CREATED");
        ArgumentCaptor<GlAccountExternalRef> saved = ArgumentCaptor.forClass(GlAccountExternalRef.class);
        verify(refs).saveAndFlush(saved.capture());
        assertThat(saved.getValue().getPayloadHash()).isEqualTo(PayloadHasher.sha256(body));
    }

    @Test
    @DisplayName("parent not synced yet: 409 and nothing written")
    void rejectsUnsyncedParent() {
        String body = BODY.replace("}", ",\"parentAccount\":\"GL-1000\"}");
        when(refs.findByExternalReference("GL-1200")).thenReturn(Optional.empty());
        when(glAccounts.findOneByGlCode("1200")).thenReturn(Optional.empty());
        when(refs.findByExternalReference("GL-1000")).thenReturn(Optional.empty());

        assertThatThrownBy(() -> service.sync("GL-1200", command(body))).isInstanceOf(GlAccountSyncConflictException.class)
                .hasFieldOrPropertyWithValue("globalisationMessageCode", "error.msg.glaccount.sync.parent.not.synced");
        verify(coreGlAccountService, never()).createGLAccount(any());
        verify(refs, never()).saveAndFlush(any());
    }

    @Test
    @DisplayName("parent is a DETAIL (sub-ledger) account: 409, only HEADER accounts can have children")
    void rejectsDetailParent() {
        String body = BODY.replace("}", ",\"parentAccount\":\"GL-1100\"}");
        when(refs.findByExternalReference("GL-1200")).thenReturn(Optional.empty());
        when(glAccounts.findOneByGlCode("1200")).thenReturn(Optional.empty());
        when(refs.findByExternalReference("GL-1100")).thenReturn(Optional.of(new GlAccountExternalRef(8L, "GL-1100", "hash")));
        GLAccount detail = mock(GLAccount.class);
        when(detail.isHeaderAccount()).thenReturn(false);
        when(glAccounts.findById(8L)).thenReturn(Optional.of(detail));

        assertThatThrownBy(() -> service.sync("GL-1200", command(body))).isInstanceOf(GlAccountSyncConflictException.class)
                .hasFieldOrPropertyWithValue("globalisationMessageCode", "error.msg.glaccount.sync.parent.not.header");
        verify(coreGlAccountService, never()).createGLAccount(any());
    }

    @Test
    @DisplayName("parent of another type: 409, a child must have its parent's type (core doesn't check this)")
    void rejectsParentOfAnotherType() {
        String body = BODY.replace("}", ",\"parentAccount\":\"GL-4000\"}");
        when(refs.findByExternalReference("GL-1200")).thenReturn(Optional.empty());
        when(glAccounts.findOneByGlCode("1200")).thenReturn(Optional.empty());
        when(refs.findByExternalReference("GL-4000")).thenReturn(Optional.of(new GlAccountExternalRef(9L, "GL-4000", "hash")));
        GLAccount incomeHeader = mock(GLAccount.class);
        when(incomeHeader.isHeaderAccount()).thenReturn(true);
        when(incomeHeader.getType()).thenReturn(4);
        when(glAccounts.findById(9L)).thenReturn(Optional.of(incomeHeader));

        assertThatThrownBy(() -> service.sync("GL-1200", command(body))).isInstanceOf(GlAccountSyncConflictException.class)
                .hasFieldOrPropertyWithValue("globalisationMessageCode", "error.msg.glaccount.sync.parent.type.mismatch");
        verify(coreGlAccountService, never()).createGLAccount(any());
    }

    private static GLAccount headerParent(Long id, int type, boolean header) {
        GLAccount parent = mock(GLAccount.class);
        when(parent.getId()).thenReturn(id);
        when(parent.getType()).thenReturn(type);
        when(parent.isHeaderAccount()).thenReturn(header);
        return parent;
    }

    private static JsonCommand command(String json) {
        return JsonCommand.from(json, JsonParser.parseString(json), new FromJsonHelper(), "GLACCOUNT", null, null, null, null, null, null,
                null, "/erp-accounts/GL-1200", null, null, null, null, null);
    }
}
