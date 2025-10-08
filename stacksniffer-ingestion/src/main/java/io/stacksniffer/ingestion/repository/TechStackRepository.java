package io.stacksniffer.ingestion.repository;

import io.stacksniffer.core.model.TechStackAnalysis;

/**
 * Repository interface for storing tech stack analysis data
 */
public interface TechStackRepository {
    
    /**
     * Saves tech stack analysis
     *
     * @param analysis the tech stack analysis to save
     * @return saved analysis
     */
    TechStackAnalysis save(TechStackAnalysis analysis);

    /**
     * Finds tech stack analysis by repository ID
     *
     * @param repositoryId the repository ID
     * @return tech stack analysis or null if not found
     */
    TechStackAnalysis findByRepositoryId(String repositoryId);

    /**
     * Deletes tech stack analysis by repository ID
     *
     * @param repositoryId the repository ID
     */
    void deleteByRepositoryId(String repositoryId);
}
