package com.inkomoko.fineract.glsync.handler;

import com.inkomoko.fineract.glsync.api.ErpAccountCommands;
import com.inkomoko.fineract.glsync.service.GlAccountSyncService;
import lombok.RequiredArgsConstructor;
import org.apache.fineract.commands.annotation.CommandType;
import org.apache.fineract.commands.handler.NewCommandSourceHandler;
import org.apache.fineract.infrastructure.core.api.JsonCommand;
import org.apache.fineract.infrastructure.core.data.CommandProcessingResult;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** Registered with Fineract's CommandHandlerProvider under {@code GLACCOUNT|SYNC}; permission {@code SYNC_GLACCOUNT}. */
@Service
@CommandType(entity = ErpAccountCommands.ENTITY, action = ErpAccountCommands.ACTION)
@RequiredArgsConstructor
public class SyncGlAccountCommandHandler implements NewCommandSourceHandler {

    private final GlAccountSyncService syncService;

    @Transactional
    @Override
    public CommandProcessingResult processCommand(JsonCommand command) {
        return syncService.sync(ErpAccountCommands.erpReferenceFrom(command.getUrl()), command);
    }
}
