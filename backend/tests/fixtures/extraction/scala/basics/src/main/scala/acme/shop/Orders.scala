package acme.shop

import java.util.concurrent.{ConcurrentHashMap, TimeUnit => Unit2}
import scala.collection.mutable
import scala.util.Try, scala.concurrent._

trait Priced {
  def price(qty: Int): Long
  def label(): String = "item " + price(1)
}

@SerialVersionUID(1L)
case class Item(sku: String, cents: Long) extends Priced {
  def price(qty: Int): Long = cents * qty
}

object Item {
  def apply(sku: String): Item = new Item(sku, 0L)
  def parse(raw: String): Option[Item] = Try(Item(raw)).toOption
}

class Cart(owner: String) extends Iterable[Item] with Priced {
  private val items = mutable.ListBuffer[Item]()
  private val index: Inventory = new Inventory(10)

  def this() = this("guest")

  def add(item: Item): Unit = items += item
  def add(sku: String, qty: Int): Unit = {
    def expand(n: Int): Seq[Item] = Seq.fill(n)(Item(sku))
    expand(qty) foreach add
    index.reserve(sku, qty)
  }
  def add(sku: String, cents: Long): Unit = add(new Item(sku, cents))

  override def iterator: Iterator[Item] = items.iterator
  def price(qty: Int): Long = items.map(_.price(qty)).sum
  @deprecated("use total", "2.0")
  def sum(): Long = this.price(1)
  def total(tax: Tax): Long = tax.apply(price(1))
  def audit(): Unit = Audit.record(owner, label())
}

class Inventory(capacity: Int) {
  def reserve(sku: String, qty: Int): Boolean = qty <= capacity
  def release(sku: String): Unit = {
    val listener = new Runnable {
      def run(): Unit = println(sku)
    }
    listener.run()
  }
}

class Tax(rate: Double) {
  def apply(cents: Long): Long = math.round(cents * (1 + rate))
}

object Audit {
  def record(who: String, what: String): Unit = log(s"$who: $what")
  private def log(line: String): Unit = Console.println(line)
}

class GiftCart extends Cart("gift") {
  override def add(item: Item): Unit = {
    super.add(item)
    Audit.record("gift", item.sku)
  }
}

object Checkout {
  def run(cart: Cart, tax: Tax): Long = {
    cart.add("sku-1", 2)
    cart.add("sku-2", 150L)
    cart.total(tax)
  }
}
