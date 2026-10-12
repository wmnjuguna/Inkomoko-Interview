package com.inkomoko.fineract.glsync.api;

import com.google.gson.JsonElement;
import com.google.gson.JsonParseException;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;
import org.apache.fineract.accounting.glaccount.domain.GLAccountType;
import org.apache.fineract.accounting.glaccount.domain.GLAccountUsage;
import org.apache.fineract.commands.domain.CommandWrapper;
import org.apache.fineract.infrastructure.core.data.ApiParameterError;
import org.apache.fineract.infrastructure.core.exception.PlatformApiDataValidationException;

/** Builds the GLACCOUNT/SYNC command. The ERP reference travels in the command's resource URL; the body stays core's GL body. */
public final class ErpAccountCommands {

    public static final String ENTITY = "GLACCOUNT";
    public static final String ACTION = "SYNC";
    static final String HREF_PREFIX = "/erp-integration/accounts/";
    public static final String ERP_REFERENCE_PATTERN = "^[A-Za-z0-9._:-]{1,100}$";
    private static final Pattern ERP_REFERENCE = Pattern.compile(ERP_REFERENCE_PATTERN);

    private ErpAccountCommands() {}

    public static CommandWrapper syncCommand(String erpReference, String json) {
        validate(erpReference);
        validateClassification(json);
        validateParent(erpReference, json);
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
     * Account type and usage decide where a GL account sits in the chart of accounts and which loan product roles it can
     * fill. The acc_gl_account table only has NOT NULL on those columns, no range check, so the allowed values are
     * enforced here, before Fineract processes anything. The ranges come from core's own enums and the error codes are
     * core's, so the contract matches core POST /glaccounts. Usage is required here even though core's validator treats it
     * as optional, because the column is NOT NULL.
     */
    static void validateClassification(String json) {
        JsonElement body;
        try {
            body = JsonParser.parseString(json == null ? "" : json);
        } catch (JsonParseException e) {
            return; // malformed JSON is reported by core's own parser
        }
        if (!body.isJsonObject()) {
            return;
        }
        List<ApiParameterError> errors = new ArrayList<>();
        checkInRange(body.getAsJsonObject(), "type", GLAccountType.getMinValue(), GLAccountType.getMaxValue(), errors);
        checkInRange(body.getAsJsonObject(), "usage", GLAccountUsage.getMinValue(), GLAccountUsage.getMaxValue(), errors);
        if (!errors.isEmpty()) {
            throw new PlatformApiDataValidationException("validation.msg.validation.errors.exist", "Validation errors exist.", errors);
        }
    }

    /**
     * The parent is named by its ERP reference, never by a Fineract id, so Fineract ids stay out of the contract. The
     * module resolves it to core's parentId.
     */
    static void validateParent(String erpReference, String json) {
        JsonObject body = parseObject(json);
        if (body == null) {
            return;
        }
        List<ApiParameterError> errors = new ArrayList<>();
        if (body.has("parentId")) {
            errors.add(ApiParameterError.parameterError("validation.msg.erpaccount.parentId.not.supported",
                    "Name the parent with `parentAccount`, not a Fineract `parentId`.", "parentId", (Object) body.get("parentId").toString()));
        }
        JsonElement parent = body.get("parentAccount");
        if (parent != null && !parent.isJsonNull()) {
            String parentReference = parent.isJsonPrimitive() ? parent.getAsString().trim() : "";
            if (!ERP_REFERENCE.matcher(parentReference).matches()) {
                errors.add(ApiParameterError.parameterError("validation.msg.erpaccount.parentAccount.invalid",
                        "parentAccount must be 1-100 characters of letters, digits, '.', '_', ':' or '-'", "parentAccount",
                        (Object) parent.toString()));
            } else if (parentReference.equals(erpReference)) {
                errors.add(ApiParameterError.parameterError("validation.msg.erpaccount.parentAccount.self",
                        "An account can't be its own parent.", "parentAccount", (Object) parentReference));
            }
        }
        if (!errors.isEmpty()) {
            throw new PlatformApiDataValidationException("validation.msg.validation.errors.exist", "Validation errors exist.", errors);
        }
    }

    private static JsonObject parseObject(String json) {
        try {
            JsonElement body = JsonParser.parseString(json == null ? "" : json);
            return body.isJsonObject() ? body.getAsJsonObject() : null;
        } catch (JsonParseException e) {
            return null; // malformed JSON is reported by core's own parser
        }
    }

    private static void checkInRange(JsonObject body, String field, int min, int max, List<ApiParameterError> errors) {
        JsonElement value = body.get(field);
        if (value == null || value.isJsonNull() || (value.isJsonPrimitive() && value.getAsString().isBlank())) {
            errors.add(ApiParameterError.parameterError("validation.msg.GLAccount." + field + ".cannot.be.blank",
                    "The parameter `" + field + "` is mandatory.", field));
            return;
        }
        Integer number = value.isJsonPrimitive() ? parseInteger(value.getAsString().trim()) : null;
        if (number == null) {
            errors.add(ApiParameterError.parameterError("validation.msg.invalid.integer",
                    "The parameter `" + field + "` must be a whole number.", field, (Object) value.toString()));
        } else if (number < min || number > max) {
            errors.add(ApiParameterError.parameterError("validation.msg.GLAccount." + field + ".is.not.within.expected.range",
                    "The parameter `" + field + "` must be between " + min + " and " + max + ".", field, number, min, max));
        }
    }

    private static Integer parseInteger(String text) {
        try {
            return Integer.valueOf(text);
        } catch (NumberFormatException e) {
            return null;
        }
    }

    static void validate(String erpReference) {
        if (erpReference == null || !ERP_REFERENCE.matcher(erpReference).matches()) {
            throw new PlatformApiDataValidationException(List.of(ApiParameterError.parameterError(
                    "validation.msg.erpaccount.erpReference.invalid",
                    "erpReference must be 1-100 characters of letters, digits, '.', '_', ':' or '-'", "erpReference", (Object) erpReference)));
        }
    }
}
