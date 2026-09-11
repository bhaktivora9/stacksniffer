package io.stacksniffer.repository;

import java.util.Objects;

/** Repository-side projection. The contract value is opaque and never derived here. */
public record ClassificationContractProjection(String classificationContractId) {
    public ClassificationContractProjection {
        if (classificationContractId == null || !classificationContractId.matches("sha256:[0-9a-f]{64}")) {
            throw new IllegalArgumentException("classificationContractId must be an opaque contract identity");
        }
    }

    public boolean isCurrent(String candidateContractId) {
        return Objects.equals(classificationContractId, candidateContractId);
    }
}
