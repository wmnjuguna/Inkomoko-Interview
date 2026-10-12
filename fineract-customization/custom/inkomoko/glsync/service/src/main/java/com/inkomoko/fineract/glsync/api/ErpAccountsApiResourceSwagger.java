package com.inkomoko.fineract.glsync.api;

import io.swagger.v3.oas.annotations.media.ArraySchema;
import io.swagger.v3.oas.annotations.media.Schema;
import io.swagger.v3.oas.annotations.media.Schema.AdditionalPropertiesValue;
import io.swagger.v3.oas.annotations.media.Schema.RequiredMode;

/**
 * OpenAPI-only schemas for {@link ErpAccountsApiResource}, following Fineract's {@code *ApiResourceSwagger} convention.
 *
 * <p>Field constraints are what Fineract 1.15.0 actually enforces: core's {@code GLAccountCommand.validateForCreate}, plus
 * the {@code acc_gl_account} column definitions where the database is stricter than the validator. The response types
 * are not here: {@link ErpAccountSyncResult} and {@link ErpAccount} are the records the endpoint really returns.
 */
final class ErpAccountsApiResourceSwagger {

    private ErpAccountsApiResourceSwagger() {}

    @Schema(name = "SyncErpAccountRequest", additionalProperties = AdditionalPropertiesValue.FALSE, description = """
            One account from the ERP chart of accounts, in Fineract's GL account shape. These are exactly the fields of core \
            POST /glaccounts and are validated by core; anything else (for example the ERP's `currency`) is rejected with 400. \
            The ERP reference is not in the body: it is the `{erpReference}` path segment.""")
    record SyncErpAccountRequest(

            @Schema(requiredMode = RequiredMode.REQUIRED, minLength = 1, maxLength = 200, example = "Loan Receivable", description = "Account name, shown in Fineract's chart of accounts. Stored in `acc_gl_account.name`.") String name,

            @Schema(requiredMode = RequiredMode.REQUIRED, minLength = 1, maxLength = 45, example = "1200", description = "GL code, unique across all GL accounts in the tenant. A code already used by another account returns 409. Stored in `acc_gl_account.gl_code`.") String glCode,

            @Schema(requiredMode = RequiredMode.REQUIRED, allowableValues = { "1", "2", "3", "4",
                    "5" }, example = "1", description = "Account type: `1` ASSET, `2` LIABILITY, `3` EQUITY, `4` INCOME, `5` EXPENSE. Checked by this module before Fineract processes the request, because `acc_gl_account` has no range constraint on it. The Integration Service maps the ERP's type names to these values.") Integer type,

            @Schema(requiredMode = RequiredMode.REQUIRED, allowableValues = { "1",
                    "2" }, example = "1", description = "`1` DETAIL (transactions post to it), `2` HEADER (groups other accounts). Core's validator treats it as optional, but the column is NOT NULL, so this API requires it.") Integer usage,

            @Schema(requiredMode = RequiredMode.REQUIRED, example = "false", description = "Whether manual journal entries may post to this account. ERP-owned accounts normally use `false`.") Boolean manualEntriesAllowed,

            @Schema(maxLength = 500, example = "Mirrored from ERP account GL-1200", description = "Optional free text.") String description,

            @Schema(minimum = "1", example = "10", description = "Optional Fineract id of the parent account. It must exist (404) and be a HEADER account (403).") Long parentId,

            @Schema(minimum = "1", example = "3", description = "Optional tag: a code value from the tag code list for this account type (404 if unknown).") Long tagId) {
    }

    @Schema(name = "FineractError", description = "Fineract's standard error envelope, used for every 4xx response.")
    record FineractError(

            @Schema(example = "409", description = "The HTTP status, repeated as a string.") String httpStatusCode,

            @Schema(example = "error.msg.glaccount.sync.payload.conflict", description = "Stable machine-readable code. Clients should branch on this, not on the message.") String userMessageGlobalisationCode,

            @Schema(example = "External reference GL-1200 is already synced with a different payload") String defaultUserMessage,

            @Schema(example = "The request conflicts with an existing GL account.") String developerMessage,

            @ArraySchema(schema = @Schema(implementation = FineractErrorDetail.class), arraySchema = @Schema(description = "One entry per failed rule.")) FineractErrorDetail[] errors) {
    }

    @Schema(name = "FineractErrorDetail", description = "One failed rule.")
    record FineractErrorDetail(

            @Schema(example = "validation.msg.GLAccount.glCode.cannot.be.blank") String userMessageGlobalisationCode,

            @Schema(example = "glCode", description = "The request field the rule applies to, when there is one.") String parameterName,

            @Schema(example = "The parameter `glCode` is mandatory.") String defaultUserMessage,

            @Schema(example = "The parameter `glCode` is mandatory.") String developerMessage) {
    }
}
