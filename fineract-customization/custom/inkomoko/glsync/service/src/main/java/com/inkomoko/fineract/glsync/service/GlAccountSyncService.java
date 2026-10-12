package com.inkomoko.fineract.glsync.service;

import com.inkomoko.fineract.glsync.domain.GlAccountExternalRef;
import com.inkomoko.fineract.glsync.domain.GlAccountExternalRefRepository;
import com.inkomoko.fineract.glsync.domain.PayloadHasher;
import com.inkomoko.fineract.glsync.exception.GlAccountSyncConflictException;
import java.util.Map;
import java.util.Optional;
import lombok.RequiredArgsConstructor;
import org.apache.commons.lang3.StringUtils;
import lombok.extern.slf4j.Slf4j;
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

        CommandProcessingResult created = coreGlAccountService.createGLAccount(command);
        refs.saveAndFlush(new GlAccountExternalRef(created.getResourceId(), erpReference, payloadHash));
        log.info("GL sync created: {} -> GL account {}", erpReference, created.getResourceId());
        return resultFor(command, erpReference, glCode, created.getResourceId(), "CREATED");
    }

    private static CommandProcessingResult resultFor(JsonCommand command, String erpReference, String glCode, Long glAccountId,
            String outcome) {
        return new CommandProcessingResultBuilder().withCommandId(command.commandId()).withEntityId(glAccountId)
                .with(Map.of("erpReference", erpReference, "glCode", glCode, "outcome", outcome)).build();
    }
}
