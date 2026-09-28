package com.acme.orders.service;

import static java.util.Objects.requireNonNull;

import com.acme.orders.model.LineItem;
import com.acme.orders.model.Order;
import org.springframework.stereotype.Service;

@Service
public class OrderService {
    private final PricingPolicy pricing;

    public OrderService(PricingPolicy pricing) {
        this.pricing = requireNonNull(pricing);
    }

    public Order place(String customer, String sku, int quantity) {
        Order order = new Order(customer);
        order.add(sku, quantity);
        return order;
    }

    public long total(Order order) {
        long sum = 0;
        for (LineItem item : order.items()) {
            sum += pricing.price(item);
        }
        return sum;
    }
}
