import { EventEmitter } from 'node:events';
import { retry } from './retry.js';

export class Bus extends EventEmitter {
  static #instance = null;

  static get() {
    Bus.#instance ??= new Bus();
    return Bus.#instance;
  }

  publish(topic, payload) {
    const envelope = this.#wrap(topic, payload);
    this.emit(topic, envelope);
    return envelope;
  }

  #wrap(topic, payload) {
    return { topic, payload, id: nextId() };
  }
}

let counter = 0;
function nextId() {
  counter += 1;
  return `evt-${counter}`;
}

export class DurableBus extends Bus {
  constructor(store) {
    super();
    this.store = store;
  }

  publish(topic, payload) {
    const envelope = super.publish(topic, payload);
    retry(() => this.store.save(envelope), 3);
    return envelope;
  }
}

export const subscribe = (topic, handler) => Bus.get().on(topic, handler);
