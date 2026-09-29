class Order < ApplicationRecord
  has_many :line_items
  validates :email, presence: true

  def total
    line_items.sum(&:amount)
  end

  def self.recent(limit = 10)
    order(created_at: :desc).limit(limit)
  end
end
