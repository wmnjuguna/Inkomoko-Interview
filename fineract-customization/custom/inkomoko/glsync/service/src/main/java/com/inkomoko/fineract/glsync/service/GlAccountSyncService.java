package com.inkomoko.fineract.glsync.service;

import com.inkomoko.fineract.glsync.domain.GlAccountExternalRef;
import com.inkomoko.fineract.glsync.domain.GlAccountExternalRefRepository;
import com.inkomoko.fineract.glsync.domain.PayloadHasher;
import com.inkomoko.fineract.glsync.exception.GlAccountSyncConflictException;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import lombok.RequiredArgsConstructor;
import org.apache.commons.lang3.StringUtils;
import lombok.extern.slf4j.Slf4j;
import org.apache.fineract.accounting.glaccount.domain.GLAccount;
import org.apache.fineract.accounting.glaccount.domain.GLAccountRepository;
import org.apache.fineract.accounting.glaccount.service.GLAccountWritePlatformService;
import org.apache.fineract.infrastructure.core.api.JsonCommand;
import org.apache.fineract.infrastructure.core.data.CommandProcessingResult;
import org.apache.fineract.infrastructure.core.data.CommandProcessingResultBuilder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * The GL sync rules. Runs inside the command transaction Fineract opens in
 * {@code CommandSourceService.processCommandAndSaveResult}, so the GL account, the reference row and Fineract's audit
 * result commit together or not at all.
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class GlAccountSyncService {

    static final String PARENT_ACCOUNT = "parentAccount";

    private final AdvisoryLocks locks;
    private final GlAccountExternalRefRepository refs;
    private final GLAccountRepository glAccounts;
    private final GLAccountWritePlatformService coreGlAccountService;

    @Transactional
    public CommandProcessingResult sync(String erpReference, JsonCommand command) {
        String glCode = StringUtils.trimToNull(command.stringValueOfParameterNamed("glCode")); // core stores it trimmed
        String payloadHash = PayloadHasher.sha256(command.json());
        locks.lockInOrder("glsync:ref:" + erpReference, "glsync:code:" + glCode);

        Optional<GlAccountExternalRef> existing = refs.findByExternalReference(erpReference);
        if (existing.isPresent()) {
            if (!existing.get().getPayloadHash().equals(payloadHash)) {
                throw GlAccountSyncConflictException.payloadMismatch(erpReference);
            }
            log.info("GL sync replay: {} already maps to GL account {}", erpReference, existing.get().getGlAccountId());
            return resultFor(command, erpReference, glCode, existing.get().getGlAccountId(), "UNCHANGED");
        }
        if (glCode != null && glAccounts.findOneByGlCode(glCode).isPresent()) {
            throw GlAccountSyncConflictException.codeTaken(glCode);
        }

        String parentAccount = StringUtils.trimToNull(command.stringValueOfParameterNamed(PARENT_ACCOUNT));
        JsonCommand coreCommand = parentAccount == null ? command
                : withCoreParentId(command, resolveHeaderParent(parentAccount, command.integerValueSansLocaleOfParameterNamed("type")));

        CommandProcessingResult created = coreGlAccountService.createGLAccount(coreCommand);
        refs.saveAndFlush(new GlAccountExternalRef(created.getResourceId(), erpReference, payloadHash));
        log.info("GL sync created: {} -> GL account {}", erpReference, created.getResourceId());
        return resultFor(command, erpReference, glCode, created.getResourceId(), "CREATED");
    }

    /**
     * Header/detail rule: a parent must be a HEADER account (a ledger heading nothing posts to) of the same type. Core
     * checks the header rule but not the type, so a child INCOME account could otherwise sit under an ASSET heading.
     */
    private GLAccount resolveHeaderParent(String parentAccount, Integer childType) {
        GlAccountExternalRef parentLink = refs.findByExternalReference(parentAccount)
                .orElseThrow(() -> GlAccountSyncConflictException.parentNotSynced(parentAccount));
        GLAccount parent = glAccounts.findById(parentLink.getGlAccountId())
                .orElseThrow(() -> GlAccountSyncConflictException.parentNotSynced(parentAccount));
        if (!parent.isHeaderAccount()) {
            throw GlAccountSyncConflictException.parentNotHeader(parentAccount);
        }
        if (!Objects.equals(parent.getType(), childType)) {
            throw GlAccountSyncConflictException.parentTypeMismatch(parentAccount, parent.getType(), childType);
        }
        return parent;
    }

    /** Core only understands its own parentId; swap the ERP reference for it, keeping the original command's identity. */
    private static JsonCommand withCoreParentId(JsonCommand command, GLAccount parent) {
        JsonObject body = JsonParser.parseString(command.json()).getAsJsonObject();
        body.remove(PARENT_ACCOUNT);
        body.addProperty("parentId", parent.getId());
        return JsonCommand.fromExistingCommand(command, body);
    }

    private static CommandProcessingResult resultFor(JsonCommand command, String erpReference, String glCode, Long glAccountId,
            String outcome) {
        Map<String, Object> changes = new LinkedHashMap<>();
        changes.put("erpReference", erpReference);
        changes.put("glCode", glCode);
        changes.put("outcome", outcome);
        String parentAccount = StringUtils.trimToNull(command.stringValueOfParameterNamed(PARENT_ACCOUNT));
        if (parentAccount != null) {
            changes.put("parentAccount", parentAccount);
        }
        return new CommandProcessingResultBuilder().withCommandId(command.commandId()).withEntityId(glAccountId).with(changes).build();
    }
}
