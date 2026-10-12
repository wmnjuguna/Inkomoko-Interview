package com.inkomoko.fineract.glsync.exception;

import org.apache.fineract.infrastructure.core.exception.AbstractPlatformResourceNotFoundException;

/** No GL account has been synced for this ERP reference yet. Core maps this type to 404. */
public class ErpAccountNotFoundException extends AbstractPlatformResourceNotFoundException {

    public ErpAccountNotFoundException(String erpReference) {
        super("error.msg.erpaccount.not.found", "No GL account is synced for ERP reference " + erpReference, erpReference);
    }
}
