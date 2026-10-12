package com.inkomoko.fineract.glsync.service;

import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.mockito.InOrder;
import org.springframework.jdbc.core.JdbcTemplate;

class AdvisoryLocksTest {

    private static final String SQL = "SELECT pg_advisory_xact_lock(hashtext(?))";

    @Test
    @DisplayName("locks keys in sorted order whatever order they are passed in, so two requests can't deadlock")
    void locksInSortedOrder() {
        JdbcTemplate jdbc = mock(JdbcTemplate.class);

        new AdvisoryLocks(jdbc).lockInOrder("glsync:ref:GL-1200", "glsync:code:1200");

        InOrder order = inOrder(jdbc);
        order.verify(jdbc).queryForList(SQL, "glsync:code:1200");
        order.verify(jdbc).queryForList(SQL, "glsync:ref:GL-1200");
    }

    @Test
    @DisplayName("locks a repeated key once")
    void locksDuplicateKeyOnce() {
        JdbcTemplate jdbc = mock(JdbcTemplate.class);

        new AdvisoryLocks(jdbc).lockInOrder("glsync:ref:A", "glsync:ref:A");

        verify(jdbc, times(1)).queryForList(eq(SQL), anyString());
    }
}
