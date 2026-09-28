package com.acme.shop;

import java.util.ArrayList;
import java.util.List;
import static java.util.Objects.requireNonNull;
import com.acme.shop.model.*;

@Service
@RequestMapping("/orders")
public class OrderService extends BaseService implements Auditable, Comparable<OrderService> {
    private final List<String> items = new ArrayList<>();

    public OrderService(String name) {
        super(name);
        requireNonNull(name);
    }

    @Override
    public int compareTo(OrderService other) {
        return Integer.compare(size(), other.size());
    }

    public int size() {
        return items.size();
    }

    public void add(String item) {
        add(item, 1);
    }

    public void add(String item, int quantity) {
        for (int i = 0; i < quantity; i++) {
            this.items.add(item);
        }
        audit("add");
    }

    public Runnable task() {
        return new Runnable() {
            @Override
            public void run() {
                size();
            }
        };
    }

    static class Line {
        String sku;
    }
}

abstract class BaseService {
    protected BaseService(String name) {}

    protected void audit(String action) {
        log(action);
    }

    private static void log(String message) {
        System.out.println(message);
    }
}

interface Auditable extends java.io.Serializable {
    default String auditName() {
        return getClass().getSimpleName();
    }
}

enum Status {
    OPEN, CLOSED;

    boolean isOpen() {
        return this == OPEN;
    }
}

record Money(long cents) {}
