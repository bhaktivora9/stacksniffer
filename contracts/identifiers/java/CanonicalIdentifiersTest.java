package io.stacksniffer.contracts.identifiers;

import java.util.HashMap;
import java.util.Map;

public final class CanonicalIdentifiersTest {
    public static void main(String[] args) {
        CanonicalIdentifiers.Bundle bundle = CanonicalIdentifiers.Bundle.create("github/acme/widgets", "v1.0.0");
        if (bundle.toMap().size() != 11) {
            throw new AssertionError("expected all canonical fields");
        }
        if (!CanonicalIdentifiers.Bundle.fromMap(bundle.toMap()).equals(bundle)) {
            throw new AssertionError("bundle map round-trip failed");
        }
        if (!CanonicalIdentifiers.Bundle.fromJson(bundle.toJson()).equals(bundle)) {
            throw new AssertionError("bundle JSON round-trip failed");
        }
        expectInvalid(() -> CanonicalIdentifiers.validateRepositoryKey("GitHub/acme/widgets"));
        expectInvalid(() -> CanonicalIdentifiers.validateUuid("event_id", "123E4567-E89B-42D3-A456-426614174000"));
        expectInvalid(() -> CanonicalIdentifiers.validateTaxonomyVersion("v01.2.3"));

        Map<String, Object> unknownField = new HashMap<>(bundle.toMap());
        unknownField.put("unknown", "value");
        expectInvalid(() -> CanonicalIdentifiers.Bundle.fromMap(unknownField));
    }

    private static void expectInvalid(Runnable operation) {
        try {
            operation.run();
            throw new AssertionError("expected validation failure");
        } catch (IllegalArgumentException expected) {
            // Expected validation result.
        }
    }
}
