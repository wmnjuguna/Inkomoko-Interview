package com.inkomoko.fineract.glsync.integration;

import static org.assertj.core.api.Assertions.assertThat;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.ThreadLocalRandom;
import java.util.function.Function;
import java.util.stream.Collectors;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;

/**
 * Runs against a live customised Fineract and its Postgres database (FINERACT_URL, default http://localhost:8080). Every
 * test uses fresh references and GL codes, so the suite can run repeatedly against the same database.
 */
@Tag("integration")
class ErpAccountsApiIntegrationTest {

    private static final String BASE = System.getenv().getOrDefault("FINERACT_URL", "http://localhost:8080") + "/fineract-provider/api/v1";
    private static final String ADMIN = System.getenv().getOrDefault("FINERACT_ADMIN", "mifos:password");
    private static final HttpClient HTTP = HttpClient.newHttpClient();

    private record Reply(int status, JsonObject body, HttpResponse<String> raw) {}

    @Test
    @DisplayName("behaviours 1 and 2: first sync creates the GL account, an identical repeat returns it")
    void createsThenReturnsExisting() throws Exception {
        String ref = uniqueRef();
        String code = uniqueCode();

        Reply first = put(ADMIN, ref, account(code, "Loan Receivable"));
        Reply repeat = put(ADMIN, ref, account(code, "Loan Receivable"));

        assertThat(first.status()).isEqualTo(200);
        assertThat(first.body().get("outcome").getAsString()).isEqualTo("CREATED");
        assertThat(repeat.status()).isEqualTo(200);
        assertThat(repeat.body().get("outcome").getAsString()).isEqualTo("UNCHANGED");
        assertThat(repeat.body().get("glAccountId").getAsLong()).isEqualTo(first.body().get("glAccountId").getAsLong());
        assertThat(glAccountsWithCode(code)).isEqualTo(1);

        Reply lookup = get(ADMIN, "/erp-accounts/" + ref);
        assertThat(lookup.status()).isEqualTo(200);
        assertThat(lookup.body().get("glAccountId").getAsLong()).isEqualTo(first.body().get("glAccountId").getAsLong());
        assertThat(lookup.body().get("name").getAsString()).isEqualTo("Loan Receivable");
    }

    @Test
    @DisplayName("behaviour 3: same reference with a different payload is rejected with 409 and changes nothing")
    void rejectsDifferentPayload() throws Exception {
        String ref = uniqueRef();
        String code = uniqueCode();
        put(ADMIN, ref, account(code, "Loan Receivable"));

        Reply conflict = put(ADMIN, ref, account(code, "Loans Receivable"));

        assertThat(conflict.status()).isEqualTo(409);
        assertThat(conflict.body().get("userMessageGlobalisationCode").getAsString()).isEqualTo("error.msg.glaccount.sync.payload.conflict");
        assertThat(get(ADMIN, "/erp-accounts/" + ref).body().get("name").getAsString()).isEqualTo("Loan Receivable");
    }

    @Test
    @DisplayName("GL code uniqueness: a new reference cannot reuse an existing code")
    void rejectsTakenCode() throws Exception {
        String code = uniqueCode();
        put(ADMIN, uniqueRef(), account(code, "Bank"));

        String secondRef = uniqueRef();
        Reply conflict = put(ADMIN, secondRef, account(code, "Bank"));

        assertThat(conflict.status()).isEqualTo(409);
        assertThat(conflict.body().get("userMessageGlobalisationCode").getAsString()).isEqualTo("error.msg.glaccount.sync.code.conflict");
        assertThat(get(ADMIN, "/erp-accounts/" + secondRef).status()).isEqualTo(404);
        assertThat(glAccountsWithCode(code)).isEqualTo(1);
    }

    @Test
    @DisplayName("behaviour 4: a user without SYNC_GLACCOUNT gets 403 from Fineract's permission model")
    void enforcesPermission() throws Exception {
        String reader = createUserWithPermissions("READ_GLACCOUNT");

        Reply denied = put(reader, uniqueRef(), account(uniqueCode(), "Interest Income"));

        assertThat(denied.status()).isEqualTo(403);
        assertThat(denied.body().get("userMessageGlobalisationCode").getAsString()).isEqualTo("error.msg.not.authorized");
    }

    @Test
    @DisplayName("10 parallel identical syncs create exactly one GL account")
    void parallelSyncsCreateOneAccount() throws Exception {
        String ref = uniqueRef();
        String code = uniqueCode();
        String body = account(code, "Fee Income");

        ExecutorService pool = Executors.newFixedThreadPool(10);
        List<Callable<Reply>> calls = new ArrayList<>();
        for (int i = 0; i < 10; i++) {
            calls.add(() -> put(ADMIN, ref, body));
        }
        List<Reply> replies = new ArrayList<>();
        for (Future<Reply> future : pool.invokeAll(calls)) {
            replies.add(future.get());
        }
        pool.shutdown();

        assertThat(replies).allSatisfy(reply -> assertThat(reply.status()).isEqualTo(200));
        Map<String, Long> outcomes = replies.stream()
                .collect(Collectors.groupingBy(reply -> reply.body().get("outcome").getAsString(), Collectors.counting()));
        assertThat(outcomes).containsEntry("CREATED", 1L).containsEntry("UNCHANGED", 9L);
        assertThat(replies.stream().map(reply -> reply.body().get("glAccountId").getAsLong()).distinct()).hasSize(1);
        assertThat(glAccountsWithCode(code)).isEqualTo(1);
    }

    @Test
    @DisplayName("a repeated Idempotency-Key replays the original response from Fineract's command store")
    void replaysIdempotencyKey() throws Exception {
        String ref = uniqueRef();
        String body = account(uniqueCode(), "Bank");
        String key = "SYNC:" + ref + ":1";

        Reply first = send("PUT", ADMIN, "/erp-accounts/" + ref, body, Map.of("Idempotency-Key", key));
        Reply replay = send("PUT", ADMIN, "/erp-accounts/" + ref, body, Map.of("Idempotency-Key", key));

        assertThat(replay.status()).isEqualTo(200);
        assertThat(replay.raw().headers().firstValue("x-served-from-cache")).contains("true");
        assertThat(replay.body()).isEqualTo(first.body());
    }

    @Test
    @DisplayName("unknown reference: 404; invalid reference or missing usage: 400")
    void rejectsUnknownAndInvalid() throws Exception {
        assertThat(get(ADMIN, "/erp-accounts/" + uniqueRef()).status()).isEqualTo(404);
        assertThat(put(ADMIN, "bad%20ref", account(uniqueCode(), "Bank")).status()).isEqualTo(400);

        Reply noUsage = put(ADMIN, uniqueRef(), "{\"name\":\"Bank\",\"glCode\":\"" + uniqueCode() + "\",\"type\":1,\"manualEntriesAllowed\":false}");
        assertThat(noUsage.status()).isEqualTo(400);
        assertThat(noUsage.body().getAsJsonArray("errors").get(0).getAsJsonObject().get("parameterName").getAsString()).isEqualTo("usage");
    }

    @Test
    @DisplayName("only Fineract's account types (1-5) and usages (1-2) are accepted; nothing is written otherwise")
    void rejectsUnsupportedTypesAndUsages() throws Exception {
        Map<String, String> invalid = Map.of( //
                "\"type\":0,\"usage\":1", "validation.msg.GLAccount.type.is.not.within.expected.range", //
                "\"type\":6,\"usage\":1", "validation.msg.GLAccount.type.is.not.within.expected.range", //
                "\"type\":\"ASSET\",\"usage\":1", "validation.msg.invalid.integer", //
                "\"type\":null,\"usage\":1", "validation.msg.GLAccount.type.cannot.be.blank", //
                "\"type\":1,\"usage\":0", "validation.msg.GLAccount.usage.is.not.within.expected.range", //
                "\"type\":1,\"usage\":3", "validation.msg.GLAccount.usage.is.not.within.expected.range");

        for (Map.Entry<String, String> attempt : invalid.entrySet()) {
            String ref = uniqueRef();
            String code = uniqueCode();
            Reply reply = put(ADMIN, ref, "{\"name\":\"Type check\",\"glCode\":\"" + code + "\"," + attempt.getKey() + ",\"manualEntriesAllowed\":false}");

            assertThat(reply.status()).as(attempt.getKey()).isEqualTo(400);
            assertThat(reply.body().getAsJsonArray("errors").get(0).getAsJsonObject().get("userMessageGlobalisationCode").getAsString())
                    .as(attempt.getKey()).isEqualTo(attempt.getValue());
            assertThat(get(ADMIN, "/erp-accounts/" + ref).status()).as(attempt.getKey()).isEqualTo(404);
            assertThat(glAccountsWithCode(code)).as(attempt.getKey()).isZero();
        }
    }

    // --- helpers

    private static String account(String code, String name) {
        return "{\"name\":\"" + name + "\",\"glCode\":\"" + code + "\",\"type\":1,\"usage\":1,\"manualEntriesAllowed\":false}";
    }

    private static Reply put(String user, String ref, String body) throws Exception {
        return send("PUT", user, "/erp-accounts/" + ref, body, Map.of());
    }

    private static Reply get(String user, String path) throws Exception {
        return send("GET", user, path, null, Map.of());
    }

    private static Reply send(String method, String user, String path, String body, Map<String, String> headers) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(BASE + path))
                .header("Authorization", "Basic " + Base64.getEncoder().encodeToString(user.getBytes(StandardCharsets.UTF_8)))
                .header("Fineract-Platform-TenantId", "default").header("Content-Type", "application/json")
                .method(method, body == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(body));
        headers.forEach(request::header);
        HttpResponse<String> response = HTTP.send(request.build(), HttpResponse.BodyHandlers.ofString());
        JsonElement parsed = response.body().isBlank() ? new JsonObject() : JsonParser.parseString(response.body());
        return new Reply(response.statusCode(), parsed.isJsonObject() ? parsed.getAsJsonObject() : new JsonObject(), response);
    }

    private static long glAccountsWithCode(String code) throws Exception {
        HttpResponse<String> response = HTTP.send(HttpRequest.newBuilder(URI.create(BASE + "/glaccounts"))
                .header("Authorization", "Basic " + Base64.getEncoder().encodeToString(ADMIN.getBytes(StandardCharsets.UTF_8)))
                .header("Fineract-Platform-TenantId", "default").GET().build(), HttpResponse.BodyHandlers.ofString());
        JsonArray accounts = JsonParser.parseString(response.body()).getAsJsonArray();
        Function<JsonElement, String> glCode = account -> account.getAsJsonObject().get("glCode").getAsString();
        return accounts.asList().stream().map(glCode).filter(code::equals).count();
    }

    /** Creates a throwaway role and user (test values for this local Fineract only) and returns its basic-auth credentials. */
    private static String createUserWithPermissions(String... permissions) throws Exception {
        String suffix = Long.toString(ThreadLocalRandom.current().nextLong(1_000_000_000L), 36);
        Reply role = send("POST", ADMIN, "/roles", "{\"name\":\"it-" + suffix + "\",\"description\":\"integration test\"}", Map.of());
        long roleId = role.body().get("resourceId").getAsLong();
        String grants = java.util.Arrays.stream(permissions).map(p -> "\"" + p + "\":true").collect(Collectors.joining(","));
        send("PUT", ADMIN, "/roles/" + roleId + "/permissions", "{\"permissions\":{" + grants + "}}", Map.of());
        String password = "It!Gl5ync#Test2026q"; // satisfies Fineract's policy: no consecutive repeated characters
        send("POST", ADMIN, "/users", "{\"username\":\"it" + suffix + "\",\"firstname\":\"Integration\",\"lastname\":\"Test\","
                + "\"email\":\"it" + suffix + "@example.test\",\"officeId\":1,\"roles\":[" + roleId + "],\"sendPasswordToEmail\":false,"
                + "\"password\":\"" + password + "\",\"repeatPassword\":\"" + password + "\",\"passwordNeverExpires\":true}", Map.of());
        return "it" + suffix + ":" + password;
    }

    private static String uniqueRef() {
        return "IT-" + Long.toString(ThreadLocalRandom.current().nextLong(Long.MAX_VALUE), 36);
    }

    private static String uniqueCode() {
        return "9" + (100_000_000L + ThreadLocalRandom.current().nextLong(899_999_999L));
    }
}
