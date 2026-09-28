package com.acme.orders.service;

import com.acme.orders.model.LineItem;

public class DiscountPricing extends FlatPricing {
    public DiscountPricing(long cents) {
        super(cents);
    }

    @Override
    public long price(LineItem item) {
        return super.price(item) * 9 / 10;
    }
}
