package streams.processor

import scala.collection.mutable.ArrayBuffer
import java.time.{Duration, Instant}

abstract class Node(val name: String) {
  def process(key: String, value: String): Unit
  def close(): Unit = {}
}

class SourceNode(name: String, topic: String) extends Node(name) {
  private val children = ArrayBuffer.empty[Node]

  def addChild(child: Node): SourceNode = {
    children += child
    this
  }

  def process(key: String, value: String): Unit =
    children.foreach(_.process(key, value))

  override def close(): Unit = {
    children.clear()
    super.close()
  }
}

class MapNode(name: String, fn: String => String) extends Node(name) {
  def process(key: String, value: String): Unit = forward(key, fn(value))
  private def forward(key: String, value: String): Unit = Topology.log(name, key)
}

object Topology {
  private var started: Instant = Instant.now()

  def build(topic: String): SourceNode = {
    val source = new SourceNode("source", topic)
    source.addChild(new MapNode("upper", _.toUpperCase))
    val window: Duration = Duration.ofSeconds(30)
    log("built", window.toString())
    source
  }

  def log(node: String, key: String): Unit = println(s"$node:$key")
}
