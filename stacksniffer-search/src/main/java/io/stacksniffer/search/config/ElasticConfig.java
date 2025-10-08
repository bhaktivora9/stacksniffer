package io.stacksniffer.search.config;

/**
 * Configuration class for Elasticsearch
 */
public class ElasticConfig {
    
    private String host;
    private int port;
    private String username;
    private String password;
    private String indexPrefix;

    public ElasticConfig() {
        this.host = "localhost";
        this.port = 9200;
        this.indexPrefix = "stacksniffer";
    }

    public String getHost() {
        return host;
    }

    public void setHost(String host) {
        this.host = host;
    }

    public int getPort() {
        return port;
    }

    public void setPort(int port) {
        this.port = port;
    }

    public String getUsername() {
        return username;
    }

    public void setUsername(String username) {
        this.username = username;
    }

    public String getPassword() {
        return password;
    }

    public void setPassword(String password) {
        this.password = password;
    }

    public String getIndexPrefix() {
        return indexPrefix;
    }

    public void setIndexPrefix(String indexPrefix) {
        this.indexPrefix = indexPrefix;
    }
}
