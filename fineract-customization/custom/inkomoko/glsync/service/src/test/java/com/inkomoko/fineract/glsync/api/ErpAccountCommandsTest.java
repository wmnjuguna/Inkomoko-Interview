package com.inkomoko.fineract.glsync.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.apache.fineract.commands.domain.CommandWrapper;
import org.apache.fineract.infrastructure.core.exception.PlatformApiDataValidationException;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class ErpAccountCommandsTest {

    private static final String BODY = """
            {"name":"Loan Receivable","glCode":"1200","type":1,"usage":1,"manualEntriesAllowed":false}""";

    @Test
    @DisplayName("builds a GLACCOUNT/SYNC command guarded by SYNC_GLACCOUNT, carrying the reference in its URL")
    void buildsSyncCommand() {
        CommandWrapper command = ErpAccountCommands.syncCommand("GL-1200", BODY);

        assertThat(command.getEntityName()).isEqualTo("GLACCOUNT");
        assertThat(command.getActionName()).isEqualTo("SYNC");
        assertThat(command.getTaskPermissionName()).isEqualTo("SYNC_GLACCOUNT");
        assertThat(command.getHref()).isEqualTo("/erp-accounts/GL-1200");
        assertThat(command.getJson()).isEqualTo(BODY);
        assertThat(ErpAccountCommands.erpReferenceFrom(command.getHref())).isEqualTo("GL-1200");
    }

    @ParameterizedTest
    @ValueSource(strings = { "GL-1200", "1100", "acct.4000_v2", "ERP:GL:1200" })
    @DisplayName("accepts references made of letters, digits and . _ : -")
    void acceptsValidReferences(String reference) {
        assertThatCode(() -> ErpAccountCommands.syncCommand(reference, BODY)).doesNotThrowAnyException();
    }

    @ParameterizedTest
    @ValueSource(strings = { "", "GL 1200", "GL/1200", "GL-1200?x=1" })
    @DisplayName("rejects references with other characters")
    void rejectsInvalidReferences(String reference) {
        assertThatThrownBy(() -> ErpAccountCommands.syncCommand(reference, BODY)).isInstanceOf(PlatformApiDataValidationException.class);
    }

    @Test
    @DisplayName("rejects references longer than 100 characters (the column size)")
    void rejectsTooLongReference() {
        assertThatThrownBy(() -> ErpAccountCommands.syncCommand("G".repeat(101), BODY))
                .isInstanceOf(PlatformApiDataValidationException.class);
        assertThatCode(() -> ErpAccountCommands.syncCommand("G".repeat(100), BODY)).doesNotThrowAnyException();
    }

    @ParameterizedTest
    @ValueSource(strings = { """
            {"name":"Bank","glCode":"1100","type":1,"manualEntriesAllowed":false}""", """
            {"name":"Bank","glCode":"1100","type":1,"usage":null,"manualEntriesAllowed":false}""" })
    @DisplayName("requires usage, because acc_gl_account.account_usage is NOT NULL")
    void requiresUsage(String body) {
        assertThatThrownBy(() -> ErpAccountCommands.syncCommand("GL-1100", body)).isInstanceOf(PlatformApiDataValidationException.class)
                .satisfies(e -> assertThat(((PlatformApiDataValidationException) e).getErrors().get(0).getParameterName()).isEqualTo("usage"));
    }

    @Test
    @DisplayName("leaves malformed JSON to core's own parser")
    void malformedJsonLeftToCore() {
        assertThatCode(() -> ErpAccountCommands.syncCommand("GL-1100", "{not json")).doesNotThrowAnyException();
    }

    @Test
    @DisplayName("refuses to read a reference from a URL that isn't an ERP account command")
    void rejectsForeignUrl() {
        assertThatThrownBy(() -> ErpAccountCommands.erpReferenceFrom("/glaccounts/42")).isInstanceOf(IllegalStateException.class);
    }
}
