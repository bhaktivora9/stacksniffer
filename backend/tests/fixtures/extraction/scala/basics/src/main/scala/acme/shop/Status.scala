package acme.shop

import scala.concurrent.duration.*

enum Status:
  case Open, Closed
  def isOpen: Boolean = this == Open

def describe(status: Status): String =
  if status.isOpen then label("open") else label("closed")

def label(text: String): String = text.toUpperCase()

@main def report(): Unit =
  val status = Status.Open
  println(describe(status))
