package com.acme.inventory;

import java.util.List;

public record Shipment(List<String> skus, int each) {
    public int quantity(String sku) {
        return skus.contains(sku) ? each : 0;
    }
}
