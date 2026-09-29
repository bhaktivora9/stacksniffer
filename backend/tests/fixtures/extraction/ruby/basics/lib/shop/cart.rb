require 'json'
require_relative 'pricing'

module Shop
  module Auditable
    def audit(event)
      log("audit: #{event}")
    end

    def log(line)
      puts line
    end
  end

  class Item
    attr_reader :sku, :cents

    def initialize(sku, cents = 0)
      @sku = sku
      @cents = cents
    end

    def price(qty)
      cents * qty
    end

    def self.parse(raw)
      new(raw)
    end
  end

  class Cart
    include Auditable

    def initialize
      @items = []
    end

    def add(item)
      @items << item
      audit("add #{item.sku}")
      self
    end

    # @param item [Item]
    def line(item, qty)
      Pricing.round(item.price(qty))
    end

    def total
      subtotal - discount
    end

    def to_json(*args)
      JSON.generate(items: @items.map(&:sku), total: total)
    end

    class << self
      def from(skus)
        skus.each_with_object(new) { |sku, cart| cart.add(Item.parse(sku)) }
      end
    end

    private

    def subtotal
      @items.sum { |i| line(i, 1) }
    end

    def discount
      Pricing.discount_for(subtotal)
    end
  end

  class GiftCart < Cart
    def add(item)
      super
      log("gift #{item.sku}")
    end
  end
end
