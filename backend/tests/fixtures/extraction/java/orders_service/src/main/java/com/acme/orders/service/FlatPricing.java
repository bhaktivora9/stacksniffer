package com.acme.orders.service;

import com.acme.orders.model.LineItem;

public class FlatPricing implements PricingPolicy {
    private final long cents;

    public FlatPricing(long cents) {
        this.cents = cents;
    }

    @Override
    public long price(LineItem item) {
        return item.subtotal(cents);
    }
}
