module Receipts
  def self.render(amount, id)
    format('%s: %.2f', id, amount)
  end
end
