// config.js — Edite só aqui quando precisar trocar endereços
window.BRN_CONFIG = {
  ESCROW_FACTORY_ADDRESS: "0x5C305aCFF5cDFAee90276c2acEA4Aa841f7062d8",
  TOKEN_BRN_ADDRESS:      "0xdBc1c747B1D4c27113F65A4620b8fEaC74e2A210",
  TOKEN_USDC_ADDRESS:     "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",

  BRN_DECIMALS:  18,
  USDC_DECIMALS: 6,
  CHAIN_ID:      137,          // Polygon Mainnet
  CHAIN_HEX:     "0x89",
  CHAIN_NAME:    "Polygon Mainnet",

  RPC_FALLBACK: [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
    "https://polygon-rpc.com"
  ]
};