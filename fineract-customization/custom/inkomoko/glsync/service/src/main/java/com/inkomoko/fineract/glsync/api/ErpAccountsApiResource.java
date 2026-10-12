package com.inkomoko.fineract.glsync.api;

import com.inkomoko.fineract.glsync.service.ErpAccountReadService;
import com.inkomoko.fineract.glsync.service.ErpAccountReadService.ErpAccountView;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.Parameter;
import io.swagger.v3.oas.annotations.headers.Header;
import io.swagger.v3.oas.annotations.media.Content;
import io.swagger.v3.oas.annotations.media.ExampleObject;
import io.swagger.v3.oas.annotations.media.Schema;
import io.swagger.v3.oas.annotations.parameters.RequestBody;
import io.swagger.v3.oas.annotations.responses.ApiResponse;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.ws.rs.Consumes;
import jakarta.ws.rs.GET;
import jakarta.ws.rs.PUT;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.PathParam;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import lombok.RequiredArgsConstructor;
import org.apache.fineract.commands.service.PortfolioCommandSourceWritePlatformService;
import org.apache.fineract.infrastructure.core.data.CommandProcessingResult;
import org.apache.fineract.infrastructure.core.exception.AbstractIdempotentCommandException;
import org.apache.fineract.infrastructure.core.exception.IdempotentCommandProcessSucceedException;
import org.apache.fineract.infrastructure.security.service.PlatformSecurityContext;
import org.springframework.stereotype.Component;

@Path("/v1/erp-accounts")
@Component
@Tag(name = "ERP Accounts", description = """
        GL accounts mirrored from the ERP chart of accounts, keyed by the ERP reference. \
        The ERP owns the chart of accounts; Fineract holds a matching GL account for each ERP account.""")
@RequiredArgsConstructor
public class ErpAccountsApiResource {

    private static final String READ_PERMISSION_RESOURCE = "GLACCOUNT";

    private final PlatformSecurityContext context;
    private final PortfolioCommandSourceWritePlatformService commandsSourceWritePlatformService;
    private final ErpAccountReadService readService;

    @PUT
    @Path("{erpReference}")
    @Consumes({ MediaType.APPLICATION_JSON })
    @Produces({ MediaType.APPLICATION_JSON })
    @Operation(operationId = "syncErpAccount", summary = "Create the GL account for an ERP account, or return the one already created", description = """
            The body uses the same fields and validation as core POST /glaccounts.
            - New ERP reference, GL code free: creates the GL account. 200, outcome CREATED.
            - Same ERP reference, same payload (key order, whitespace and null fields ignored): 200, outcome UNCHANGED, nothing written.
            - Same ERP reference, different payload: 409, nothing written.
            - New ERP reference, GL code already used by another account: 409, nothing written.
            Requires permission SYNC_GLACCOUNT. Send an Idempotency-Key header to have a retry return the original response \
            (with header x-served-from-cache: true); a retry while the first call is still running returns 425.""")
    @RequestBody(required = true, content = @Content(schema = @Schema(implementation = ErpAccountsApiResourceSwagger.SyncErpAccountRequest.class), examples = @ExampleObject(name = "Loan Receivable", value = """
            {"name": "Loan Receivable", "glCode": "1200", "type": 1, "usage": 1, "manualEntriesAllowed": false}""")))
    @ApiResponse(responseCode = "200", description = "Created, or already synced with the same payload", headers = @Header(name = "x-served-from-cache", description = "true when the body is a replay for a repeated Idempotency-Key", schema = @Schema(type = "string")), content = @Content(schema = @Schema(implementation = ErpAccountSyncResult.class), examples = {
            @ExampleObject(name = "created", value = """
                    {"erpReference": "GL-1200", "glAccountId": 42, "glCode": "1200", "outcome": "CREATED"}"""),
            @ExampleObject(name = "unchanged", value = """
                    {"erpReference": "GL-1200", "glAccountId": 42, "glCode": "1200", "outcome": "UNCHANGED"}""") }))
    @ApiResponse(responseCode = "400", description = "Invalid ERP reference, missing or invalid field, or a field core doesn't support", content = @Content(schema = @Schema(implementation = ErpAccountsApiResourceSwagger.FineractError.class), examples = {
            @ExampleObject(name = "missing glCode", value = """
                    {"httpStatusCode": "400", "userMessageGlobalisationCode": "validation.msg.validation.errors.exist", "errors": [{"userMessageGlobalisationCode": "validation.msg.GLAccount.glCode.cannot.be.blank", "parameterName": "glCode"}]}"""),
            @ExampleObject(name = "unsupported field", value = """
                    {"httpStatusCode": "400", "userMessageGlobalisationCode": "validation.msg.validation.errors.exist", "errors": [{"userMessageGlobalisationCode": "error.msg.parameter.unsupported", "parameterName": "currency"}]}"""),
            @ExampleObject(name = "Fineract parentId instead of parentAccount", value = """
                    {"httpStatusCode": "400", "userMessageGlobalisationCode": "validation.msg.validation.errors.exist", "errors": [{"userMessageGlobalisationCode": "validation.msg.erpaccount.parentId.not.supported", "parameterName": "parentId"}]}"""),
            @ExampleObject(name = "invalid ERP reference", value = """
                    {"httpStatusCode": "400", "errors": [{"userMessageGlobalisationCode": "validation.msg.erpaccount.erpReference.invalid", "parameterName": "erpReference"}]}""") }))
    @ApiResponse(responseCode = "401", description = "Missing or invalid credentials or tenant")
    @ApiResponse(responseCode = "403", description = "User lacks permission SYNC_GLACCOUNT, or parentId is not a HEADER account", content = @Content(schema = @Schema(implementation = ErpAccountsApiResourceSwagger.FineractError.class)))
    @ApiResponse(responseCode = "404", description = "parentId or tagId does not exist", content = @Content(schema = @Schema(implementation = ErpAccountsApiResourceSwagger.FineractError.class)))
    @ApiResponse(responseCode = "409", description = "ERP reference already synced with a different payload, GL code already in use, or the parent is not synced, not a HEADER account, or of a different type", content = @Content(schema = @Schema(implementation = ErpAccountsApiResourceSwagger.FineractError.class), examples = {
            @ExampleObject(name = "payload conflict", value = """
                    {"httpStatusCode": "409", "userMessageGlobalisationCode": "error.msg.glaccount.sync.payload.conflict", "defaultUserMessage": "External reference GL-1200 is already synced with a different payload"}"""),
            @ExampleObject(name = "GL code taken", value = """
                    {"httpStatusCode": "409", "userMessageGlobalisationCode": "error.msg.glaccount.sync.code.conflict", "defaultUserMessage": "GL code 1200 already belongs to another GL account"}"""),
            @ExampleObject(name = "parent not synced", value = """
                    {"httpStatusCode": "409", "userMessageGlobalisationCode": "error.msg.glaccount.sync.parent.not.synced", "defaultUserMessage": "Parent ERP account GL-1000 has not been synced yet; sync parents before their children"}"""),
            @ExampleObject(name = "parent is a DETAIL account", value = """
                    {"httpStatusCode": "409", "userMessageGlobalisationCode": "error.msg.glaccount.sync.parent.not.header", "defaultUserMessage": "Parent ERP account GL-1100 is a DETAIL account; only HEADER accounts can have child accounts"}"""),
            @ExampleObject(name = "parent of another type", value = """
                    {"httpStatusCode": "409", "userMessageGlobalisationCode": "error.msg.glaccount.sync.parent.type.mismatch", "defaultUserMessage": "Parent ERP account GL-1000 has type 1 but this account has type 4; a child account must have its parent's type"}""") }))
    @ApiResponse(responseCode = "425", description = "A request with the same Idempotency-Key is still being processed; retry later")
    public Response sync(@PathParam("erpReference") @Parameter(description = "The ERP's reference for the account", example = "GL-1200", schema = @Schema(type = "string", pattern = ErpAccountCommands.ERP_REFERENCE_PATTERN, minLength = 1, maxLength = 100)) String erpReference,
            @Parameter(hidden = true) String apiRequestBodyAsJson) {
        try {
            CommandProcessingResult result = commandsSourceWritePlatformService
                    .logCommandSource(ErpAccountCommands.syncCommand(erpReference, apiRequestBodyAsJson));
            return Response.ok(ErpAccountSyncResult.from(result).toJson(), MediaType.APPLICATION_JSON).build();
        } catch (IdempotentCommandProcessSucceedException replay) {
            // Fineract stored this endpoint's own response body for the key; return it unchanged.
            int status = replay.getStatusCode() == null ? 200 : replay.getStatusCode();
            return Response.status(status).type(MediaType.APPLICATION_JSON)
                    .entity(replay.getResponse())
                    .header(AbstractIdempotentCommandException.IDEMPOTENT_CACHE_HEADER, "true").build();
        }
    }

    @GET
    @Path("{erpReference}")
    @Produces({ MediaType.APPLICATION_JSON })
    @Operation(operationId = "retrieveErpAccount", summary = "Show which GL account an ERP account is mapped to", description = """
            Use this to resolve a sync whose response was lost (e.g. a timeout): if it returns 200, the sync happened. \
            Requires permission READ_GLACCOUNT.""")
    @ApiResponse(responseCode = "200", description = "Mapped", content = @Content(schema = @Schema(implementation = ErpAccount.class)))
    @ApiResponse(responseCode = "401", description = "Missing or invalid credentials or tenant")
    @ApiResponse(responseCode = "403", description = "User lacks permission READ_GLACCOUNT", content = @Content(schema = @Schema(implementation = ErpAccountsApiResourceSwagger.FineractError.class)))
    @ApiResponse(responseCode = "404", description = "No GL account is synced for this ERP reference yet", content = @Content(schema = @Schema(implementation = ErpAccountsApiResourceSwagger.FineractError.class), examples = @ExampleObject(name = "not synced", value = """
            {"httpStatusCode": "404", "userMessageGlobalisationCode": "error.msg.resource.not.found", "defaultUserMessage": "The requested resource is not available.", "errors": [{"userMessageGlobalisationCode": "error.msg.erpaccount.not.found", "defaultUserMessage": "No GL account is synced for ERP reference GL-9999"}]}""")))
    public String retrieve(@PathParam("erpReference") @Parameter(description = "The ERP's reference for the account", example = "GL-1200", schema = @Schema(type = "string", pattern = ErpAccountCommands.ERP_REFERENCE_PATTERN, minLength = 1, maxLength = 100)) String erpReference) {
        context.authenticatedUser().validateHasReadPermission(READ_PERMISSION_RESOURCE);
        ErpAccountView view = readService.retrieve(erpReference);
        return new ErpAccount(view.erpReference(), view.glAccountId(), view.glCode(), view.name(), view.type(), view.usage(),
                view.parentAccount(), view.hierarchy(), view.syncedAt()).toJson();
    }
}
