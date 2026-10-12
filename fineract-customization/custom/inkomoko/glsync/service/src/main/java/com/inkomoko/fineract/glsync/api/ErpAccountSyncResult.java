package com.inkomoko.fineract.glsync.api;

import com.google.gson.Gson;
import io.swagger.v3.oas.annotations.media.Schema;
import io.swagger.v3.oas.annotations.media.Schema.RequiredMode;
import java.util.Map;
import org.apache.fineract.infrastructure.core.data.CommandProcessingResult;

/**
 * Response of PUT /v1/erp-accounts/{erpReference}, built from Fineract's command result. For a repeated Idempotency-Key,
 * Fineract replays this exact body as it stored it.
 */
@Schema(name = "ErpAccountSyncResult", description = "Which GL account the ERP account maps to, and whether this call created it.")
public record ErpAccountSyncResult(

        @Schema(requiredMode = RequiredMode.REQUIRED, example = "GL-1200", description = "The ERP reference from the path.") String erpReference,

        @Schema(requiredMode = RequiredMode.REQUIRED, example = "42", description = "Fineract's GL account id. Use it in loan product accounting mappings.") Long glAccountId,

        @Schema(requiredMode = RequiredMode.REQUIRED, example = "1200") String glCode,

        @Schema(requiredMode = RequiredMode.REQUIRED, allowableValues = { "CREATED",
                "UNCHANGED" }, example = "CREATED", description = "`CREATED`: this call created the GL account. `UNCHANGED`: the same payload was already synced, so nothing was written.") String outcome) {

    private static final Gson GSON = new Gson();

    static ErpAccountSyncResult from(CommandProcessingResult result) {
        Map<String, Object> changes = result.getChanges();
        return new ErpAccountSyncResult((String) changes.get("erpReference"), result.getResourceId(), (String) changes.get("glCode"),
                (String) changes.get("outcome"));
    }

    String toJson() {
        return GSON.toJson(this);
    }
}
