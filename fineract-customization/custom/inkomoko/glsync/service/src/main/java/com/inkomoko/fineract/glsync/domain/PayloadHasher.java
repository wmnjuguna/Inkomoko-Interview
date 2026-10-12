package com.inkomoko.fineract.glsync.domain;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.google.gson.JsonPrimitive;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.Map;
import java.util.TreeMap;

/**
 * Hashes a GL account payload so that "the same payload" means the same business content: key order, whitespace around
 * values, null fields and number-vs-string spellings ({@code 1} and {@code "1"}) don't change the hash.
 */
public final class PayloadHasher {

    private PayloadHasher() {}

    public static String sha256(String json) {
        String canonical = canonicalJson(JsonParser.parseString(json));
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(canonical.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is not available", e);
        }
    }

    static String canonicalJson(JsonElement element) {
        return canonicalize(element).toString();
    }

    private static JsonElement canonicalize(JsonElement element) {
        if (element == null || element.isJsonNull()) {
            return null;
        }
        if (element.isJsonObject()) {
            Map<String, JsonElement> sorted = new TreeMap<>();
            for (Map.Entry<String, JsonElement> entry : element.getAsJsonObject().entrySet()) {
                JsonElement value = canonicalize(entry.getValue());
                if (value != null) {
                    sorted.put(entry.getKey(), value);
                }
            }
            JsonObject result = new JsonObject();
            sorted.forEach(result::add);
            return result;
        }
        if (element.isJsonArray()) {
            JsonArray result = new JsonArray();
            element.getAsJsonArray().forEach(item -> result.add(canonicalize(item)));
            return result;
        }
        return new JsonPrimitive(element.getAsJsonPrimitive().getAsString().trim());
    }
}
