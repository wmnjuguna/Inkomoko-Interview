package com.inkomoko.fineract.glsync.service;

import com.inkomoko.fineract.glsync.domain.GlAccountExternalRef;
import com.inkomoko.fineract.glsync.domain.GlAccountExternalRefRepository;
import com.inkomoko.fineract.glsync.exception.ErpAccountNotFoundException;
import lombok.RequiredArgsConstructor;
import org.apache.fineract.accounting.glaccount.domain.GLAccount;
import org.apache.fineract.accounting.glaccount.domain.GLAccountRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** Looks up what an ERP reference is mapped to, so a caller can resolve a timed-out sync instead of retrying blindly. */
@Service
@RequiredArgsConstructor
public class ErpAccountReadService {

    private final GlAccountExternalRefRepository refs;
    private final GLAccountRepository glAccounts;

    public record ErpAccountView(String erpReference, Long glAccountId, String glCode, String name, String syncedAt) {}

    @Transactional(readOnly = true)
    public ErpAccountView retrieve(String erpReference) {
        GlAccountExternalRef ref = refs.findByExternalReference(erpReference).orElseThrow(() -> new ErpAccountNotFoundException(erpReference));
        GLAccount glAccount = glAccounts.findById(ref.getGlAccountId()).orElseThrow(() -> new ErpAccountNotFoundException(erpReference));
        return new ErpAccountView(erpReference, glAccount.getId(), glAccount.getGlCode(), glAccount.getName(), ref.getCreatedAt().toString());
    }
}
