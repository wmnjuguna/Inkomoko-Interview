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

    public static GlAccountSyncConflictException parentNotSynced(String parentAccount) {
        return new GlAccountSyncConflictException("error.msg.glaccount.sync.parent.not.synced",
                "Parent ERP account " + parentAccount + " has not been synced yet; sync parents before their children", parentAccount);
    }

    public static GlAccountSyncConflictException parentNotHeader(String parentAccount) {
        return new GlAccountSyncConflictException("error.msg.glaccount.sync.parent.not.header",
                "Parent ERP account " + parentAccount + " is a DETAIL account; only HEADER accounts can have child accounts",
                parentAccount);
    }

    public static GlAccountSyncConflictException parentTypeMismatch(String parentAccount, Integer parentType, Integer childType) {
        return new GlAccountSyncConflictException("error.msg.glaccount.sync.parent.type.mismatch",
                "Parent ERP account " + parentAccount + " has type " + parentType + " but this account has type " + childType
                        + "; a child account must have its parent's type",
                parentAccount, parentType, childType);
    }

    public static GlAccountSyncConflictException codeTaken(String glCode) {
        return new GlAccountSyncConflictException("error.msg.glaccount.sync.code.conflict",
                "GL code " + glCode + " already belongs to another GL account", glCode);
    }
}
