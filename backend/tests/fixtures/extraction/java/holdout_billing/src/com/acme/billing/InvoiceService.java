package com.acme.billing;

import java.util.List;
import java.util.function.Function;

public class InvoiceService {
    private final TaxPolicy policy;

    public InvoiceService(TaxPolicy policy) {
        this.policy = policy;
    }

    public InvoiceService() {
        this(new FlatTax(0.2));
    }

    public long total(List<Long> amounts) {
        long sum = 0;
        for (Long amount : amounts) {
            sum += policy.apply(amount);
        }
        return Rounding.round(sum);
    }

    public long totalAll(long... amounts) {
        Function<Long, Long> taxed = amount -> policy.apply(amount);
        long sum = 0;
        for (long amount : amounts) {
            sum += taxed.apply(amount);
        }
        return sum;
    }

    static final class Rounding {
        static long round(long value) {
            return Math.round(value / 100.0) * 100;
        }
    }
}

interface TaxPolicy {
    long apply(long amount);

    default TaxPolicy twice() {
        return amount -> apply(apply(amount));
    }
}

class FlatTax implements TaxPolicy {
    private final double rate;

    FlatTax(double rate) {
        this.rate = rate;
    }

    @Override
    public long apply(long amount) {
        return amount + (long) (amount * rate);
    }
}
