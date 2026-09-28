package com.acme.orders.service;

import com.acme.orders.model.LineItem;

public interface PricingPolicy {
    long price(LineItem item);

    static PricingPolicy flat(long cents) {
        return item -> cents * item.quantity();
    }
}
