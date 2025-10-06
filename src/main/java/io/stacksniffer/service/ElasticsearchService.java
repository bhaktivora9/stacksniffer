package io.stacksniffer.service;

import co.elastic.clients.elasticsearch.ElasticsearchClient;
import co.elastic.clients.elasticsearch._types.mapping.*;
import co.elastic.clients.elasticsearch.core.*;
import co.elastic.clients.elasticsearch.core.search.Hit;
import co.elastic.clients.elasticsearch.indices.CreateIndexRequest;
import co.elastic.clients.elasticsearch.indices.ExistsRequest;
import io.stacksniffer.model.CodeChunk;
import io.stacksniffer.model.SearchRequest;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import jakarta.annotation.PostConstruct;
import java.io.IOException;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
@Slf4j
public class ElasticsearchService {

    private final ElasticsearchClient elasticsearchClient;
    private static final String INDEX_NAME = "code_chunks";
    private static final int VECTOR_DIMENSION = 768;

    @PostConstruct
    public void initializeIndex() {
        try {
            boolean exists = elasticsearchClient.indices()
                .exists(ExistsRequest.of(e -> e.index(INDEX_NAME)))
                .value();

            if (!exists) {
                createIndex();
                log.info("Created Elasticsearch index: {}", INDEX_NAME);
            }
        } catch (IOException e) {
            log.error("Error initializing Elasticsearch index", e);
        }
    }

    private void createIndex() throws IOException {
        elasticsearchClient.indices().create(CreateIndexRequest.of(c -> c
            .index(INDEX_NAME)
            .mappings(TypeMapping.of(m -> m
                .properties("id", Property.of(p -> p.keyword(KeywordProperty.of(k -> k))))
                .properties("repositoryUrl", Property.of(p -> p.keyword(KeywordProperty.of(k -> k))))
                .properties("filePath", Property.of(p -> p.text(TextProperty.of(t -> t))))
                .properties("code", Property.of(p -> p.text(TextProperty.of(t -> t))))
                .properties("vector", Property.of(p -> p.denseVector(DenseVectorProperty.of(d -> d
                    .dims(VECTOR_DIMENSION)
                    .index(true)
                    .similarity("cosine")
                ))))
                .properties("tags", Property.of(p -> p.keyword(KeywordProperty.of(k -> k))))
                .properties("language", Property.of(p -> p.keyword(KeywordProperty.of(k -> k))))
                .properties("startLine", Property.of(p -> p.integer(IntegerNumberProperty.of(i -> i))))
                .properties("endLine", Property.of(p -> p.integer(IntegerNumberProperty.of(i -> i))))
            ))
        ));
    }

    public void indexCodeChunk(CodeChunk codeChunk) throws IOException {
        if (codeChunk.getId() == null) {
            codeChunk.setId(UUID.randomUUID().toString());
        }

        elasticsearchClient.index(IndexRequest.of(i -> i
            .index(INDEX_NAME)
            .id(codeChunk.getId())
            .document(codeChunk)
        ));
        
        log.debug("Indexed code chunk: {}", codeChunk.getId());
    }

    public void bulkIndexCodeChunks(List<CodeChunk> codeChunks) throws IOException {
        BulkRequest.Builder br = new BulkRequest.Builder();

        for (CodeChunk chunk : codeChunks) {
            if (chunk.getId() == null) {
                chunk.setId(UUID.randomUUID().toString());
            }
            br.operations(op -> op
                .index(idx -> idx
                    .index(INDEX_NAME)
                    .id(chunk.getId())
                    .document(chunk)
                )
            );
        }

        BulkResponse result = elasticsearchClient.bulk(br.build());
        
        if (result.errors()) {
            log.error("Bulk indexing had errors");
        } else {
            log.info("Bulk indexed {} code chunks", codeChunks.size());
        }
    }

    public List<CodeChunk> hybridSearch(SearchRequest searchRequest, List<Float> queryVector) throws IOException {
        int topK = searchRequest.getTopK() != null ? searchRequest.getTopK() : 10;

        SearchResponse<CodeChunk> response = elasticsearchClient.search(s -> s
            .index(INDEX_NAME)
            .query(q -> q
                .bool(b -> {
                    // Text search
                    if (searchRequest.getQuery() != null && !searchRequest.getQuery().isEmpty()) {
                        b.should(sh -> sh
                            .match(m -> m
                                .field("code")
                                .query(searchRequest.getQuery())
                            )
                        );
                    }
                    
                    // Vector search (semantic)
                    if (queryVector != null && !queryVector.isEmpty()) {
                        b.should(sh -> sh
                            .scriptScore(ss -> ss
                                .query(qq -> qq.matchAll(ma -> ma))
                                .script(sc -> sc
                                    .inline(in -> in
                                        .source("cosineSimilarity(params.queryVector, 'vector') + 1.0")
                                        .params("queryVector", co.elastic.clients.json.JsonData.of(queryVector))
                                    )
                                )
                            )
                        );
                    }
                    
                    // Tag filter
                    if (searchRequest.getTags() != null && !searchRequest.getTags().isEmpty()) {
                        b.filter(f -> f
                            .terms(t -> t
                                .field("tags")
                                .terms(tt -> tt.value(searchRequest.getTags().stream()
                                    .map(tag -> co.elastic.clients.elasticsearch._types.FieldValue.of(tag))
                                    .collect(Collectors.toList())))
                            )
                        );
                    }
                    
                    // Language filter
                    if (searchRequest.getLanguage() != null) {
                        b.filter(f -> f
                            .term(t -> t
                                .field("language")
                                .value(searchRequest.getLanguage())
                            )
                        );
                    }
                    
                    return b;
                })
            )
            .size(topK)
            , CodeChunk.class
        );

        return response.hits().hits().stream()
            .map(Hit::source)
            .collect(Collectors.toList());
    }

    public List<CodeChunk> searchByRepository(String repositoryUrl) throws IOException {
        SearchResponse<CodeChunk> response = elasticsearchClient.search(s -> s
            .index(INDEX_NAME)
            .query(q -> q
                .term(t -> t
                    .field("repositoryUrl")
                    .value(repositoryUrl)
                )
            )
            .size(1000)
            , CodeChunk.class
        );

        return response.hits().hits().stream()
            .map(Hit::source)
            .collect(Collectors.toList());
    }
}
