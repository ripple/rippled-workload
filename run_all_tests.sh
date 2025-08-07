#!/bin/bash

# docker exec workload eventually_payment.sh                    # 1 Payment
docker exec workload parallel_driver_add_accounts.sh          # 1 Payment
docker exec workload parallel_driver_nftoken_create_offer.sh  # 6 NFTokenCreateOffer [done]
docker exec workload parallel_driver_nftoken_accept_offer.sh  # 7NFTokenAcceptOffer [done]
docker exec workload parallel_driver_nftoken_burn.sh          # 8NFTokenBurn [done]
docker exec workload parallel_driver_nftoken_mint.sh          # 9NFTokenMint [done]
docker exec workload parallel_driver_payment.sh               # 1 Payment [done]
docker exec workload parallel_driver_ticket.sh                # 4 TicketCreate [done]
# docker exec workload parallel_driver_trustset.sh              # 5TrustSet [done]
    # broken because needs issued currencies which in turn breaks the rest of these:
# docker exec workload parallel_driver_offer.sh                 # 3 OfferCreate [done]
# docker exec workload parallel_driver_cancel_offer.sh          # 2 OfferCancel [done]
