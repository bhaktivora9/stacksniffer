package io.stacksniffer.contracts.identifiers;

public final class DerivedIdentitiesTest {
    public static void main(String[] args) {
        String repositoryKey = "github/acme/widgets";
        String commitSha = "0123456789abcdef0123456789abcdef01234567";
        String deterministic = CanonicalIdentifiers.deterministicAnalysisKey(repositoryKey, commitSha, "det-v1");
        String contract = CanonicalIdentifiers.classificationContractId("class-v3", "gemini-fast", "prompt-v5", "v1.2.0");
        String classification = CanonicalIdentifiers.classificationKey("018f0c7e-7b2a-4c8d-9e11-223344556677", contract);
        String assembled = CanonicalIdentifiers.assembledAnalysisKey(deterministic, classification);
        String review = CanonicalIdentifiers.agentReviewSpecKey(repositoryKey, commitSha, "architecture review", "agent-v2", "prompt-v5", "gemini-fast");
        String context = CanonicalIdentifiers.agentContextFingerprint("018f0c7e-7b2a-4c8d-9e11-223344556681", "index-17", "v1.2.0", "graph-42");

        assertEquals("sha256:31f8a82ed145cdd0f3625733dfce4652f97aac4ad318e35b4a9a370aa4f59041", deterministic);
        assertEquals("sha256:1085646c1dda9eee32e4238e0247e002a0c78657bc6ecaa7882a712b22023c8d", contract);
        assertEquals("sha256:bf573249fd836d8ca54006b87f5bdeabe07200efe21a0e5ed4ff513753edaabd", classification);
        assertEquals("sha256:60276581b80667cc82fc30e6a112a60fcca39cc5539033fe80635c22a9acabb8", assembled);
        assertEquals("sha256:e9ec7c8f717316e7e1cf3ca14788cf66bb2dfd8256f94938503ef55703c8e291", review);
        assertEquals("sha256:704997c7753a2afe083405bd8f91a5cc0706151f78d4b1238dcec3880861abaf", context);
        assertEquals("sha256:905a42a346a3f926d8ffd8d726b013ac829a31f705750b7d8da10ed98cb5cea5",
                CanonicalIdentifiers.agentResultCacheKey(review, context));

        String changedContract = CanonicalIdentifiers.classificationContractId("class-v3", "gemini-fast", "prompt-v5", "v1.2.1");
        String changedClassification = CanonicalIdentifiers.classificationKey(
            "018f0c7e-7b2a-4c8d-9e11-223344556677", changedContract);
        String changedAssembled = CanonicalIdentifiers.assembledAnalysisKey(deterministic, changedClassification);
        if (!deterministic.equals(CanonicalIdentifiers.deterministicAnalysisKey(repositoryKey, commitSha, "det-v1"))
            || contract.equals(changedContract)
            || classification.equals(changedClassification)
            || assembled.equals(changedAssembled)) {
            throw new AssertionError("taxonomy change did not propagate correctly");
        }
    }

    private static void assertEquals(String expected, String actual) {
        if (!expected.equals(actual)) {
            throw new AssertionError("expected " + expected + " but got " + actual);
        }
    }
}
