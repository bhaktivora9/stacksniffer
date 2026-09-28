package com.acme.inventory;

import java.util.HashMap;
import java.util.Map;

public class Stock implements Countable {
    private final Map<String, Integer> levels = new HashMap<>();

    public void receive(String sku, int quantity) {
        levels.merge(sku, quantity, Integer::sum);
        Audit.record("receive", sku);
    }

    public void receive(Shipment shipment) {
        for (String sku : shipment.skus()) {
            receive(sku, shipment.quantity(sku));
        }
    }

    @Override
    public int count() {
        return levels.size();
    }
}

interface Countable {
    int count();

    default boolean isEmpty() {
        return count() == 0;
    }
}

final class Audit {
    private Audit() {}

    static void record(String action, String sku) {
        System.out.println(action + " " + sku);
    }
}
