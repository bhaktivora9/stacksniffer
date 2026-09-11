package io.stacksniffer.repository;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class ClassificationContractBoundaryTests {
    private static final String CONTRACT = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    private static final String NEXT_CONTRACT = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    @Test
    void repositoryComparesOpaqueContractIdsOnly() {
        ClassificationContractProjection projection = new ClassificationContractProjection(CONTRACT);
        assertTrue(projection.isCurrent(CONTRACT));
        assertFalse(projection.isCurrent(NEXT_CONTRACT));
    }

    @Test
    void supersededRunRetainsOriginalContractProvenance() {
        ClassificationRun run = new ClassificationRun("run-1", "analysis-1", CONTRACT, ClassificationRun.Status.RUNNING);
        ClassificationRun superseded = run.supersede();
        assertEquals(ClassificationRun.Status.SUPERSEDED, superseded.status());
        assertEquals(CONTRACT, superseded.classificationContractId());
        assertNotEquals(NEXT_CONTRACT, superseded.classificationContractId());
    }

    @Test
    void repositoryRejectsMalformedOpaqueIdsWithoutDerivingThem() {
        assertThrows(IllegalArgumentException.class,
                () -> new ClassificationContractProjection("class-v1/default/prompt-v1/v1.0.0"));
    }
}
