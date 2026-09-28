package com.acme.orders.api;

import com.acme.orders.model.Order;
import com.acme.orders.service.OrderService;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/orders")
public class OrderController {
    private final OrderService orders;

    public OrderController(OrderService orders) {
        this.orders = orders;
    }

    @PostMapping
    public Order create(@RequestBody CreateOrder request) {
        return orders.place(request.customer(), request.sku(), request.quantity());
    }

    @GetMapping("/{id}/total")
    public long total(@PathVariable String id) {
        Order order = lookup(id);
        return orders.total(order);
    }

    private Order lookup(String id) {
        return new Order(id);
    }

    public record CreateOrder(String customer, String sku, int quantity) {}
}
