import matplotlib.pyplot as plt
from xrpl.models.requests.book_offers import BookOffers

from workload import logging
from workload.randoms import gauss

log = logging.getLogger(__name__)

figure_size = (5, 3)

STD_DEV = 1
MEAN = 100
NUM_ORDERS = 100


def generate_prices(num_orders: int = NUM_ORDERS, mean: int = MEAN, std_dev: int = STD_DEV):
    round_to = 4  # decimal places
    data = [round(gauss(mean, std_dev), round_to) for _ in range(num_orders)]
    return data


def plot_orderbook(orderbook: list[float]):
    BookOffers.from_dict(book_offers_dict)
    plt.figure(figsize=figure_size)
    plt.hist(orderbook, bins=20, density=True, color="blue")
    plt.title("Normal Distribution")
    plt.xlabel("Value")
    plt.ylabel("Frequency")
    return plt

def main():
    orderbook = generate_prices()
    plt = plot_orderbook(orderbook)
    plt.show(block=False)
    plt.waitforbuttonpress()
    plt.close()


if __name__ == "__main__":
    main()
