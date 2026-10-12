package com.inkomoko.fineract.glsync.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import lombok.AccessLevel;
import lombok.Getter;
import lombok.NoArgsConstructor;

/**
 * Links an ERP {@code external_reference} to exactly one Fineract GL account, together with the hash of the payload
 * that created it. Plain columns only (no JPA relationships), so the entity needs none of Fineract's static weaving.
 */
@Entity
@Table(name = "glsync_external_ref")
@Getter
@NoArgsConstructor(access = AccessLevel.PROTECTED)
public class GlAccountExternalRef {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "gl_account_id", nullable = false, unique = true)
    private Long glAccountId;

    @Column(name = "external_reference", nullable = false, unique = true, length = 100)
    private String externalReference;

    @Column(name = "payload_hash", nullable = false, length = 64)
    private String payloadHash;

    @Column(name = "created_at", nullable = false)
    private OffsetDateTime createdAt;

    public GlAccountExternalRef(Long glAccountId, String externalReference, String payloadHash) {
        this.glAccountId = glAccountId;
        this.externalReference = externalReference;
        this.payloadHash = payloadHash;
        this.createdAt = OffsetDateTime.now(ZoneOffset.UTC);
    }
}
