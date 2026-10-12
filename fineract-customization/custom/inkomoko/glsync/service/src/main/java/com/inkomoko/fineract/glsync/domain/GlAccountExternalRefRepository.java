package com.inkomoko.fineract.glsync.domain;

import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

public interface GlAccountExternalRefRepository extends JpaRepository<GlAccountExternalRef, Long> {

    Optional<GlAccountExternalRef> findByExternalReference(String externalReference);

    Optional<GlAccountExternalRef> findByGlAccountId(Long glAccountId);
}
