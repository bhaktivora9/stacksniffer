require_relative '../models/order'
require 'securerandom'

class Checkout
  def initialize(order)
    @order = order
    @id = SecureRandom.uuid
  end

  def call
    Receipts.render(@order.total, @id)
  rescue StandardError => e
    report(e)
  end

  private

  def report(error)
    Rails.logger.error(error.message)
  end
end
