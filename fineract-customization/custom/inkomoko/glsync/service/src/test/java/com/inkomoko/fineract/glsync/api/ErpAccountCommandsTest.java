package com.inkomoko.fineract.glsync.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.apache.fineract.commands.domain.CommandWrapper;
import org.apache.fineract.infrastructure.core.exception.PlatformApiDataValidationException;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class ErpAccountCommandsTest {

    private static final String BODY = """
            {"name":"Loan Receivable","glCode":"1200","type":1,"usage":1,"manualEntriesAllowed":false}""";

    @Test
    @DisplayName("builds a GLACCOUNT/SYNC command guarded by SYNC_GLACCOUNT, carrying the reference in its URL")
    void buildsSyncCommand() {
        CommandWrapper command = ErpAccountCommands.syncCommand("GL-1200", BODY);

        assertThat(command.getEntityName()).isEqualTo("GLACCOUNT");
        assertThat(command.getActionName()).isEqualTo("SYNC");
        assertThat(command.getTaskPermissionName()).isEqualTo("SYNC_GLACCOUNT");
        assertThat(command.getHref()).isEqualTo("/erp-integration/accounts/GL-1200");
        assertThat(command.getJson()).isEqualTo(BODY);
        assertThat(ErpAccountCommands.erpReferenceFrom(command.getHref())).isEqualTo("GL-1200");
    }

    @ParameterizedTest
    @ValueSource(strings = { "GL-1200", "1100", "acct.4000_v2", "ERP:GL:1200" })
    @DisplayName("accepts references made of letters, digits and . _ : -")
    void acceptsValidReferences(String reference) {
        assertThatCode(() -> ErpAccountCommands.syncCommand(reference, BODY)).doesNotThrowAnyException();
    }

    @ParameterizedTest
    @ValueSource(strings = { "", "GL 1200", "GL/1200", "GL-1200?x=1" })
    @DisplayName("rejects references with other characters")
    void rejectsInvalidReferences(String reference) {
        assertThatThrownBy(() -> ErpAccountCommands.syncCommand(reference, BODY)).isInstanceOf(PlatformApiDataValidationException.class)
                .satisfies(e -> assertThat(((PlatformApiDataValidationException) e).getErrors().get(0).getParameterName())
                        .isEqualTo("erpReference"));
    }

    @Test
    @DisplayName("rejects references longer than 100 characters (the column size)")
    void rejectsTooLongReference() {
        assertThatThrownBy(() -> ErpAccountCommands.syncCommand("G".repeat(101), BODY))
                .isInstanceOf(PlatformApiDataValidationException.class);
        assertThatCode(() -> ErpAccountCommands.syncCommand("G".repeat(100), BODY)).doesNotThrowAnyException();
    }

    @ParameterizedTest
    @ValueSource(strings = { """
            {"name":"Bank","glCode":"1100","type":1,"manualEntriesAllowed":false}""", """
            {"name":"Bank","glCode":"1100","type":1,"usage":null,"manualEntriesAllowed":false}""" })
    @DisplayName("requires usage, because acc_gl_account.account_usage is NOT NULL")
    void requiresUsage(String body) {
        assertThatThrownBy(() -> ErpAccountCommands.syncCommand("GL-1100", body)).isInstanceOf(PlatformApiDataValidationException.class)
                .satisfies(e -> assertThat(((PlatformApiDataValidationException) e).getErrors().get(0).getParameterName()).isEqualTo("usage"));
    }

    @ParameterizedTest(name = "{0} -> {1}")
    @org.junit.jupiter.params.provider.CsvSource(delimiter = '|', value = { //
            "\"type\":0,\"usage\":1        | type  | validation.msg.GLAccount.type.is.not.within.expected.range", //
            "\"type\":6,\"usage\":1        | type  | validation.msg.GLAccount.type.is.not.within.expected.range", //
            "\"type\":\"ASSET\",\"usage\":1  | type  | validation.msg.invalid.integer", //
            "\"type\":1.5,\"usage\":1      | type  | validation.msg.invalid.integer", //
            "\"type\":null,\"usage\":1     | type  | validation.msg.GLAccount.type.cannot.be.blank", //
            "\"usage\":1                   | type  | validation.msg.GLAccount.type.cannot.be.blank", //
            "\"type\":1,\"usage\":0        | usage | validation.msg.GLAccount.usage.is.not.within.expected.range", //
            "\"type\":1,\"usage\":3        | usage | validation.msg.GLAccount.usage.is.not.within.expected.range", //
            "\"type\":1,\"usage\":\"DETAIL\" | usage | validation.msg.invalid.integer" })
    @DisplayName("rejects account types outside 1-5 and usages outside 1-2, with core's error codes")
    void rejectsUnsupportedClassification(String fields, String parameter, String code) {
        String body = "{\"name\":\"Bank\",\"glCode\":\"1100\"," + fields.trim() + ",\"manualEntriesAllowed\":false}";

        assertThatThrownBy(() -> ErpAccountCommands.syncCommand("GL-1100", body)).isInstanceOf(PlatformApiDataValidationException.class)
                .satisfies(e -> {
                    var error = ((PlatformApiDataValidationException) e).getErrors().get(0);
                    assertThat(error.getParameterName()).isEqualTo(parameter);
                    assertThat(error.getUserMessageGlobalisationCode()).isEqualTo(code);
                });
    }

    @ParameterizedTest
    @ValueSource(strings = { "1", "2", "3", "4", "5", "\"3\"" })
    @DisplayName("accepts every Fineract account type, as a number or a numeric string")
    void acceptsEveryAccountType(String type) {
        String body = "{\"name\":\"X\",\"glCode\":\"9\",\"type\":" + type + ",\"usage\":2,\"manualEntriesAllowed\":false}";
        assertThatCode(() -> ErpAccountCommands.syncCommand("GL-9", body)).doesNotThrowAnyException();
    }

    @Test
    @DisplayName("accepts a parent named by its ERP reference")
    void acceptsParentAccount() {
        String body = BODY.replace("}", ",\"parentAccount\":\"GL-1000\"}");
        assertThatCode(() -> ErpAccountCommands.syncCommand("GL-1200", body)).doesNotThrowAnyException();
    }

    @ParameterizedTest(name = "{0} -> {1}")
    @org.junit.jupiter.params.provider.CsvSource(delimiter = '|', value = { //
            "\"parentId\":5                   | parentId           | validation.msg.erpaccount.parentId.not.supported", //
            "\"parentAccount\":\"GL 1000\" | parentAccount | validation.msg.erpaccount.parentAccount.invalid", //
            "\"parentAccount\":\"GL-1200\" | parentAccount | validation.msg.erpaccount.parentAccount.self" })
    @DisplayName("rejects a Fineract parentId, a malformed parent reference, and an account that is its own parent")
    void rejectsInvalidParent(String field, String parameter, String code) {
        String body = BODY.replace("}", "," + field.trim() + "}");

        assertThatThrownBy(() -> ErpAccountCommands.syncCommand("GL-1200", body)).isInstanceOf(PlatformApiDataValidationException.class)
                .satisfies(e -> {
                    var error = ((PlatformApiDataValidationException) e).getErrors().get(0);
                    assertThat(error.getParameterName()).isEqualTo(parameter);
                    assertThat(error.getUserMessageGlobalisationCode()).isEqualTo(code);
                });
    }

    @Test
    @DisplayName("leaves malformed JSON to core's own parser")
    void malformedJsonLeftToCore() {
        assertThatCode(() -> ErpAccountCommands.syncCommand("GL-1100", "{not json")).doesNotThrowAnyException();
    }

    @Test
    @DisplayName("refuses to read a reference from a URL that isn't an ERP account command")
    void rejectsForeignUrl() {
        assertThatThrownBy(() -> ErpAccountCommands.erpReferenceFrom("/glaccounts/42")).isInstanceOf(IllegalStateException.class);
    }
}
