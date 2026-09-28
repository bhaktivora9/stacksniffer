package kafka.utils

import java.util.concurrent.locks.Lock

object CoreUtils {
  def swallow(action: => Unit): Unit =
    try action catch { case e: Throwable => Logging.warn(e.getMessage) }

  def inLock[T](lock: Lock)(fun: => T): T = {
    lock.lock()
    try fun finally lock.unlock()
  }
}

trait Logging {
  def info(msg: String): Unit = Logging.write("INFO", msg)
  def warn(msg: String): Unit = Logging.write("WARN", msg)
}

object Logging {
  def warn(msg: String): Unit = write("WARN", msg)
  def write(level: String, msg: String): Unit = System.err.println(s"[$level] $msg")
}
