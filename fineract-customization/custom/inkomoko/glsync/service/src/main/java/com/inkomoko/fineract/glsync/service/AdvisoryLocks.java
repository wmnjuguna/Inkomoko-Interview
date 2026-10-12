package com.inkomoko.fineract.glsync.service;

import java.util.Arrays;
import lombok.RequiredArgsConstructor;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

/**
 * Postgres transaction-scoped advisory locks. They serialise requests that touch the same key and are released
 * automatically at commit or rollback.
 *
 * <p>Uses Fineract's JdbcTemplate on purpose: inside a JPA transaction it resolves to the transaction's own connection
 * (Fineract's EclipseLink dialect hands out {@code em.unwrap(Connection.class)}), so the lock lives exactly as long as
 * the command transaction.
 */
@Component
@RequiredArgsConstructor
public class AdvisoryLocks {

    private final JdbcTemplate jdbcTemplate;

    /** Locks every key, always in sorted order, so two requests can never wait on each other in a cycle. */
    @Transactional(propagation = Propagation.MANDATORY)
    public void lockInOrder(String... keys) {
        Arrays.stream(keys).sorted().distinct()
                .forEach(key -> jdbcTemplate.queryForList("SELECT pg_advisory_xact_lock(hashtext(?))", key));
    }
}
