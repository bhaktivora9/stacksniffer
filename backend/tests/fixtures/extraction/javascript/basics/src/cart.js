import { discount, round as roundCents } from './pricing.js';
import * as audit from './audit.js';
import EventEmitter from 'events';

export class Item {
  constructor(sku, cents) {
    this.sku = sku;
    this.cents = cents;
  }

  price(qty) {
    return this.cents * qty;
  }

  static parse(raw) {
    return new Item(raw, 0);
  }
}

export class Cart extends EventEmitter {
  #items = [];
  onChange = () => this.emit('change', this.total());

  add(item) {
    this.#items.push(item);
    this.onChange();
  }

  /**
   * @param {Item} item
   * @param {number} qty
   */
  line(item, qty) {
    return roundCents(item.price(qty));
  }

  get count() {
    return this.#items.length;
  }

  total() {
    const lines = this.#items.map((i) => this.line(i, 1));
    return discount(lines.reduce((a, b) => a + b, 0));
  }
}

export class GiftCart extends Cart {
  add(item) {
    super.add(item);
    audit.record('gift', item.sku);
  }
}

export function checkout(cart) {
  const parse = globalThis.JSON5 ? globalThis.JSON5.parse : JSON.parse;
  const config = parse('{}');
  const gift = new GiftCart();
  gift.add(Item.parse('sku-1'));
  return helpers.summarize(cart, config);
}

const helpers = {
  summarize(cart, config) {
    return { total: cart.total(), config };
  },
  format: (cents) => `$${(cents / 100).toFixed(2)}`,
};
