package com.acme.orders.model;

public record LineItem(String sku, int quantity) {
    public LineItem {
        if (quantity <= 0) {
            throw new IllegalArgumentException("quantity");
        }
    }

    public long subtotal(long unitCents) {
        return unitCents * quantity;
    }
}
