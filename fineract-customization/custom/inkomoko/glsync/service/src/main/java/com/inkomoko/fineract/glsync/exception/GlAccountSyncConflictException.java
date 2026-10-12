package com.inkomoko.fineract.glsync.exception;

import org.apache.fineract.infrastructure.core.exception.AbstractPlatformDomainRuleException;

/**
 * The request can't be applied because it contradicts an existing GL account. Mapped to HTTP 409 by
 * {@link GlAccountSyncConflictExceptionMapper}; core would map the parent type to 403.
 */
public class GlAccountSyncConflictException extends AbstractPlatformDomainRuleException {

    private GlAccountSyncConflictException(String code, String message, Object... args) {
        super(code, message, args);
    }

    public static GlAccountSyncConflictException payloadMismatch(String externalReference) {
        return new GlAccountSyncConflictException("error.msg.glaccount.sync.payload.conflict",
                "External reference " + externalReference + " is already synced with a different payload", externalReference);
    }

    public static GlAccountSyncConflictException codeTaken(String glCode) {
        return new GlAccountSyncConflictException("error.msg.glaccount.sync.code.conflict",
                "GL code " + glCode + " already belongs to another GL account", glCode);
    }
}
