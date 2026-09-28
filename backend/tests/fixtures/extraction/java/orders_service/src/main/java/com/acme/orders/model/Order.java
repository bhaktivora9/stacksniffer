package com.acme.orders.model;

import java.util.ArrayList;
import java.util.List;

public class Order {
    private final String customer;
    private final List<LineItem> items = new ArrayList<>();

    public Order(String customer) {
        this.customer = customer;
    }

    public void add(LineItem item) {
        items.add(item);
    }

    public void add(String sku, int quantity) {
        add(new LineItem(sku, quantity));
    }

    public void add(String sku, long cents) {
        add(new LineItem(sku, 1));
    }

    public List<LineItem> items() {
        return items;
    }

    public String customer() {
        return customer;
    }
}
