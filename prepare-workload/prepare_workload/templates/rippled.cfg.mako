[server]
port_rpc_admin_local
port_peer
port_ws_admin_local

[port_rpc_admin_local]
port = ${ports["rpc_admin_local"]}
ip = 0.0.0.0
admin = [0.0.0.0]
protocol = http

[port_peer]
port = ${ports["peer"]}
ip = 0.0.0.0
protocol = peer

[port_ws_admin_local]
port = ${ports["ws_admin_local"]}
ip = 0.0.0.0
admin = [0.0.0.0]
protocol = ws

[node_db]
type = NuDB
path = /var/lib/rippled/db/nudb

[ledger_history]
full

[database_path]
/var/lib/rippled/db

[debug_logfile]
/var/log/rippled/debug.log

[node_size]
huge

[beta_rpc_api]
1

[rpc_startup]
{ "command": "log_level", "severity": "info" }

[ssl_verify]
0

[compression]
0

[peer_private]
${peer_private}

[signing_support]
${signing_support}

[ips_fixed]
${ips_fixed}

[validators]
${validator_public_keys}

% if use_unl:
[validator_list_sites]
${validator_list_sites}

[validator_list_keys]
${validator_list_keys}
% endif

% if is_validator:
[validation_seed]
${validation_seed}

[voting]
reference_fee = ${voting["reference_fee"]}
account_reserve = ${voting["account_reserve"]}
owner_reserve = ${voting["owner_reserve"]}
% endif

% if need_features:
[features]
AMM
AMMClawback
Batch
CheckCashMakesTrustLine
Checks
Clawback
Credentials
CryptoConditions
CryptoConditionsSuite
DeepFreeze
DeletableAccounts
DepositAuth
DepositPreauth
DID
DisallowIncoming
DynamicNFT
EnforceInvariants
Escrow
ExpandedSignerList
FeeEscalation
fix1201
fix1368
fix1373
fix1512
fix1513
fix1515
fix1523
fix1528
fix1543
fix1571
fix1578
fix1623
fix1781
fixAmendmentMajorityCalc
fixAMMOverflowOffer
fixAMMv1_1
fixAMMv1_2
fixAMMv1_3
fixCheckThreading
fixDisallowIncomingV1
fixEmptyDID
fixEnforceNFTokenTrustline
fixEnforceNFTokenTrustlineV2
fixFillOrKill
fixFrozenLPTokenTransfer
fixInnerObjTemplate
fixInnerObjTemplate2
fixInvalidTxFlags
fixMasterKeyAsRegularKey
fixNFTokenPageLinks
fixNFTokenRemint
fixNFTokenReserve
fixNonFungibleTokensV1_2
fixPayChanCancelAfter
fixPayChanRecipientOwnerDir
fixPreviousTxnID
fixQualityUpperBound
fixReducedOffersV1
fixReducedOffersV2
fixRemoveNFTokenAutoTrustLine
fixRmSmallIncreasedQOffers
fixSTAmountCanonicalize
fixTakerDryOfferRemoval
fixTrustLinesToSelf
fixUniversalNumber
fixXChainRewardRounding
Flow
FlowCross
FlowSortStrands
HardenedValidations
ImmediateOfferKilled
MPTokensV1
MultiSign
MultiSignReserve
NegativeUNL
NFTokenMintOffer
NonFungibleTokensV1_1
PayChan
PermissionedDEX
PermissionedDomains
PriceOracle
RequireFullyCanonicalSig
SortedDirectories
TicketBatch
TickSize
TokenEscrow
TrustSetAuth
XChainBridge
XRPFees
% endif
