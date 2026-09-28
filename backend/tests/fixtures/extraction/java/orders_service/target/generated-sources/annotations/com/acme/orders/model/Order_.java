package com.acme.orders.model;

import javax.annotation.processing.Generated;

@Generated("org.hibernate.jpamodelgen.JPAMetaModelEntityProcessor")
public abstract class Order_ {
    public static volatile String customer;

    public static String describe() {
        return customer.trim();
    }
}
