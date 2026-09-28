package streams.state

import streams.processor.{Node, Topology}

trait KeyValueStore[K, V] {
  def put(key: K, value: V): Unit
  def get(key: K): Option[V]
}

class InMemoryStore(storeName: String) extends Node(storeName) with KeyValueStore[String, String] {
  private val data = scala.collection.mutable.HashMap.empty[String, String]

  def put(key: String, value: String): Unit = data.update(key, value)
  def get(key: String): Option[String] = data.get(key)
  def process(key: String, value: String): Unit = {
    put(key, value)
    Topology.log(storeName, key)
  }
}

object Stores {
  def inMemory(name: String): KeyValueStore[String, String] = new InMemoryStore(name)
  def wrap(store: InMemoryStore): Unit = store.process("k", "v")
  def lookup(store: KeyValueStore[String, String], key: String): Option[String] = store.get(key)
}
