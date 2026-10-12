package com.inkomoko.fineract.glsync.api;

import com.google.gson.Gson;
import io.swagger.v3.oas.annotations.media.Schema;
import io.swagger.v3.oas.annotations.media.Schema.RequiredMode;

/** Response of GET /v1/erp-accounts/{erpReference}: the GL account an ERP account is mapped to. */
@Schema(name = "ErpAccount", description = "An ERP account as mirrored in Fineract. A 200 here proves an earlier sync succeeded.")
public record ErpAccount(

        @Schema(requiredMode = RequiredMode.REQUIRED, example = "GL-1200") String erpReference,

        @Schema(requiredMode = RequiredMode.REQUIRED, example = "42", description = "Fineract's GL account id.") Long glAccountId,

        @Schema(requiredMode = RequiredMode.REQUIRED, example = "1200") String glCode,

        @Schema(requiredMode = RequiredMode.REQUIRED, example = "Loan Receivable", description = "The name as currently stored in Fineract.") String name,

        @Schema(requiredMode = RequiredMode.REQUIRED, example = "2026-10-12T14:30:00Z", description = "When the mapping was created, UTC, ISO-8601.") String syncedAt) {

    private static final Gson GSON = new Gson();

    String toJson() {
        return GSON.toJson(this);
    }
}
