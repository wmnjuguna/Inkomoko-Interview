package com.inkomoko.fineract.glsync.domain;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

class PayloadHasherTest {

    private static final String LOAN_RECEIVABLE = """
            {"name":"Loan Receivable","glCode":"1200","type":1,"usage":1,"manualEntriesAllowed":false}""";

    @Test
    @DisplayName("key order does not change the hash")
    void keyOrderIgnored() {
        String reordered = """
                {"manualEntriesAllowed":false,"usage":1,"type":1,"glCode":"1200","name":"Loan Receivable"}""";
        assertThat(PayloadHasher.sha256(reordered)).isEqualTo(PayloadHasher.sha256(LOAN_RECEIVABLE));
    }

    @Test
    @DisplayName("whitespace around values does not change the hash")
    void surroundingWhitespaceIgnored() {
        String padded = """
                { "name": "  Loan Receivable ", "glCode": " 1200", "type": 1, "usage": 1, "manualEntriesAllowed": false }""";
        assertThat(PayloadHasher.sha256(padded)).isEqualTo(PayloadHasher.sha256(LOAN_RECEIVABLE));
    }

    @Test
    @DisplayName("a null field is the same as an absent one")
    void nullEqualsAbsent() {
        String withNull = """
                {"name":"Loan Receivable","glCode":"1200","type":1,"usage":1,"manualEntriesAllowed":false,"description":null}""";
        assertThat(PayloadHasher.sha256(withNull)).isEqualTo(PayloadHasher.sha256(LOAN_RECEIVABLE));
    }

    @Test
    @DisplayName("1 and \"1\" hash the same, as core accepts both")
    void numberAndNumericStringEqual() {
        String quoted = """
                {"name":"Loan Receivable","glCode":"1200","type":"1","usage":"1","manualEntriesAllowed":"false"}""";
        assertThat(PayloadHasher.sha256(quoted)).isEqualTo(PayloadHasher.sha256(LOAN_RECEIVABLE));
    }

    @Test
    @DisplayName("a changed value changes the hash")
    void changedValueChangesHash() {
        String renamed = LOAN_RECEIVABLE.replace("Loan Receivable", "Loans Receivable");
        assertThat(PayloadHasher.sha256(renamed)).isNotEqualTo(PayloadHasher.sha256(LOAN_RECEIVABLE));
    }

    @Test
    @DisplayName("the hash is 64 lowercase hex characters (fits CHAR(64))")
    void hashShape() {
        assertThat(PayloadHasher.sha256(LOAN_RECEIVABLE)).matches("[0-9a-f]{64}");
    }
}
