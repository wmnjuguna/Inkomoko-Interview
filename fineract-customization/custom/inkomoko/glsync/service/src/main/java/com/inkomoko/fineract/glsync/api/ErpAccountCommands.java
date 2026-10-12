package com.inkomoko.fineract.glsync.api;

import com.google.gson.JsonElement;
import com.google.gson.JsonParseException;
import com.google.gson.JsonParser;
import java.util.List;
import java.util.regex.Pattern;
import org.apache.fineract.commands.domain.CommandWrapper;
import org.apache.fineract.infrastructure.core.data.ApiParameterError;
import org.apache.fineract.infrastructure.core.exception.PlatformApiDataValidationException;

/** Builds the GLACCOUNT/SYNC command. The ERP reference travels in the command's resource URL; the body stays core's GL body. */
public final class ErpAccountCommands {

    public static final String ENTITY = "GLACCOUNT";
    public static final String ACTION = "SYNC";
    static final String HREF_PREFIX = "/erp-accounts/";
    public static final String ERP_REFERENCE_PATTERN = "^[A-Za-z0-9._:-]{1,100}$";
    private static final Pattern ERP_REFERENCE = Pattern.compile(ERP_REFERENCE_PATTERN);

    private ErpAccountCommands() {}

    public static CommandWrapper syncCommand(String erpReference, String json) {
        validate(erpReference);
        requireUsage(json);
        return new CommandWrapper(null, null, null, null, null, ACTION, ENTITY, null, null, HREF_PREFIX + erpReference, json, null, null,
                null, null, null, null, null, null, null);
    }

    public static String erpReferenceFrom(String href) {
        if (href == null || !href.startsWith(HREF_PREFIX)) {
            throw new IllegalStateException("Not an ERP account command URL: " + href);
        }
        return href.substring(HREF_PREFIX.length());
    }

    /**
     * Core's validator treats usage as optional, but acc_gl_account.account_usage is NOT NULL, so a missing value would
     * fail at insert time. Reject it up front with the same error shape core uses.
     */
    static void requireUsage(String json) {
        JsonElement body;
        try {
            body = JsonParser.parseString(json == null ? "" : json);
        } catch (JsonParseException e) {
            return; // malformed JSON is reported by core's own parser
        }
        if (body.isJsonObject() && (!body.getAsJsonObject().has("usage") || body.getAsJsonObject().get("usage").isJsonNull())) {
            throw new PlatformApiDataValidationException("validation.msg.validation.errors.exist", "Validation errors exist.",
                    List.of(ApiParameterError.parameterError("validation.msg.GLAccount.usage.cannot.be.blank", "The parameter `usage` is mandatory.",
                            "usage")));
        }
    }

    static void validate(String erpReference) {
        if (erpReference == null || !ERP_REFERENCE.matcher(erpReference).matches()) {
            throw new PlatformApiDataValidationException(List.of(ApiParameterError.parameterError(
                    "validation.msg.erpaccount.erpReference.invalid",
                    "erpReference must be 1-100 characters of letters, digits, '.', '_', ':' or '-'", "erpReference", erpReference)));
        }
    }
}
