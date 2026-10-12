package com.inkomoko.fineract.glsync.exception;

import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import jakarta.ws.rs.ext.ExceptionMapper;
import jakarta.ws.rs.ext.Provider;
import java.util.List;
import lombok.extern.slf4j.Slf4j;
import org.apache.fineract.infrastructure.core.data.ApiGlobalErrorResponse;
import org.apache.fineract.infrastructure.core.data.ApiParameterError;
import org.apache.fineract.infrastructure.core.exceptionmapper.FineractExceptionMapper;
import org.springframework.context.annotation.Scope;
import org.springframework.stereotype.Component;

/**
 * Returns 409 with Fineract's standard error envelope. Being typed to the concrete exception, it is preferred by Jersey
 * over core's 403 domain-rule mapper, and Fineract's ErrorHandler uses it to store the same status on the audit row.
 */
@Provider
@Component
@Scope("singleton")
@Slf4j
public class GlAccountSyncConflictExceptionMapper implements FineractExceptionMapper, ExceptionMapper<GlAccountSyncConflictException> {

    private static final int CONFLICT = 409;

    @Override
    public Response toResponse(GlAccountSyncConflictException exception) {
        log.warn("GL sync conflict: {}", exception.getDefaultUserMessage());
        ApiGlobalErrorResponse body = ApiGlobalErrorResponse.create(CONFLICT, exception.getGlobalisationMessageCode(),
                "The request conflicts with an existing GL account.", exception.getDefaultUserMessage(),
                List.of(ApiParameterError.generalError(exception.getGlobalisationMessageCode(), exception.getDefaultUserMessage(),
                        exception.getDefaultUserMessageArgs())));
        return Response.status(CONFLICT).entity(body).type(MediaType.APPLICATION_JSON).build();
    }

    @Override
    public int errorCode() {
        return 4091;
    }
}
