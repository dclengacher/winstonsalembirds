# ICAO 3-letter airline/operator codes -> airline name, for classifying
# ADS-B flight callsigns as commercial vs. general aviation on the
# Planes Detected page. Not exhaustive -- weighted toward carriers likely
# to be seen over the North Carolina Piedmont (Charlotte/CLT is an American
# Airlines hub; Piedmont Triad Intl and Charlotte both see heavy regional
# traffic feeding those majors), plus the other common US/Canada mainline
# carriers named in the ask.
#
# Keys are the real 3-letter ICAO operator code as it appears at the start
# of a readsb `flight` callsign (e.g. "DAL1423" -> "DAL"). IATA 2-letter
# codes ("DL", "AA", ...) are a different scheme and won't match here --
# that's expected, readsb/ADS-B callsigns use ICAO codes.

AIRLINE_CODES = {
    # Delta
    "DAL": "Delta Air Lines",
    "END": "Endeavor Air",

    # American
    "AAL": "American Airlines",
    "ENY": "Envoy Air",
    "PDT": "Piedmont Airlines",
    "RPA": "Republic Airways",
    "JIA": "PSA Airlines",

    # United
    "UAL": "United Airlines",
    "UCA": "CommutAir",
    "GJS": "GoJet Airlines",
    "SKW": "SkyWest Airlines",
    "AWI": "Air Wisconsin",
    "ASH": "Mesa Airlines",

    # Southwest
    "SWA": "Southwest Airlines",

    # JetBlue
    "JBU": "JetBlue Airways",

    # Allegiant
    "AAY": "Allegiant Air",

    # Spirit
    "NKS": "Spirit Airlines",

    # Alaska
    "ASA": "Alaska Airlines",
    "QXE": "Horizon Air",

    # Frontier
    "FFT": "Frontier Airlines",

    # Cargo majors
    "FDX": "FedEx Express",
    "UPS": "UPS Airlines",
    "GTI": "Atlas Air",
    "ABX": "ABX Air",
    "AJT": "Amerijet International",
    "BOX": "AeroLogic",
    "CKS": "Kalitta Air",
    "GEC": "Lufthansa Cargo",

    # Canada
    "ACA": "Air Canada",
    "JZA": "Air Canada Jazz",
    "ROU": "Air Canada Rouge",
    "WJA": "WestJet",
    "TSC": "Air Transat",
    "POE": "Porter Airlines",
    "FLE": "Flair Airlines",

    # Other mainline / low-cost US
    "HAL": "Hawaiian Airlines",
    "SCX": "Sun Country Airlines",
    "VXP": "Avelo Airlines",
    "MXY": "Breeze Airways",

    # Business / fractional / charter operators frequently seen on ADS-B
    "EJA": "NetJets",
    "XOJ": "XOJET",
    "LXJ": "Flexjet",
    "JTL": "Jet Linx Aviation",

    # Government / military (occasionally logged, worth naming rather
    # than leaving as "Unknown")
    "RCH": "US Air Force (AMC airlift)",
    "SAM": "US Government (SAM)",
    "CNV": "US Navy",

    # International widebodies that transit North Carolina airspace
    "BAW": "British Airways",
    "VIR": "Virgin Atlantic",
    "AFR": "Air France",
    "DLH": "Lufthansa",
    "KLM": "KLM Royal Dutch Airlines",
    "IBE": "Iberia",
    "TAP": "TAP Air Portugal",
    "SWR": "Swiss International Air Lines",
    "AUA": "Austrian Airlines",
    "CLX": "Cargolux",
    "ETD": "Etihad Airways",
    "UAE": "Emirates",
    "QTR": "Qatar Airways",
    "SIA": "Singapore Airlines",
    "ANA": "All Nippon Airways",
    "JAL": "Japan Airlines",
    "CPA": "Cathay Pacific",
    "KAL": "Korean Air",
    "AAR": "Asiana Airlines",
    "LAN": "LATAM Airlines",
    "AVA": "Avianca",
    "CMP": "Copa Airlines",
    "AMX": "Aeromexico",
    "VOI": "Volaris",

    # Additional smaller regional operators
    "KAP": "Contour Airlines",
    "OAW": "Southern Airways Express",
    "SIL": "Silver Airways",
}
