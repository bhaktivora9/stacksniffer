package com.acme.orders.legacy;

import com.acme.orders.model.Order;

public class LegacyImporter {
    public Order convert(String row) {
        return new Order(row.trim());
    }

    public void broken(String row {
        convert(row);
    }

    public int count(String[] rows) {
        return rows.length;
    }
}
