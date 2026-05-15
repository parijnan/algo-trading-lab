# Documentation Update Plan: Workflows & Compliance

## Objective
Upon completion of the Slack Circuit Breaker and Position Sizing implementations, the root `README.md` must be updated to accurately reflect these new operational workflows. Additionally, explicit declarations of regulatory compliance must be added to demonstrate the laboratory's adherence to recent SEBI circulars.

## Implementation Steps

### Step 1: Document Slack Workflows
Add a new subsection under the existing "Slack Messaging" or "Infrastructure" section detailing the remote management capabilities:

*   **Circuit Breaker Workflow:** Document the persistent `#actions` Control Panel, explaining the functionality of the interactive buttons (`Exit Trade`, `Kill Switch`, `Disable Algo`, `Start Leto`, `Clear Flag`) and how they interact with `leto.py` and the active strategies.
*   **Position Sizing Workflow:** Document the `⚙️ Position Sizing` button and the resulting Modal UI. Explain the 5-minute Monday morning transition window (e.g., exiting Athena at 10:25, verifying P&L, updating lot size via the modal, clearing the flag, and manually starting Leto for the 10:30 entry).

### Step 2: Add SEBI Compliance Section
Create a dedicated `## Regulatory Compliance` section highlighting the structural safety and regulatory adherence of the strategies.

*   **ELM & Calendar Spread Margin Compliance:** 
    *   State that the system is in complete compliance with circular **SEBI/HO/MRD/TPD-1/P/CIR/2024/132** regarding the removal of Calendar Spread margin benefits on expiry day and increased ELM requirements.
    *   Highlight that Artemis actively rolls its hedge inward and exits additional lots on the day prior to expiry, and Athena enforces a hard pre-expiry exit at 10:25 AM the day before expiry specifically to adhere to this mandate.
    *   Provide link: [Measures to strengthen Equity Index Derivatives Framework](https://www.sebi.gov.in/legal/circulars/oct-2024/measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html)

*   **Retail Algorithmic Trading Compliance:**
    *   State that the system is in complete compliance with circular **SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013** regarding safer participation of retail investors in algorithmic trading.
    *   Highlight the system's robust manual intervention tools (Slack Circuit Breakers), comprehensive logging, execution safeguards (ghost order protection, iterative lot splitting), and clear human-bot separation.
    *   Provide link: [Safer participation of retail investors in Algorithmic Trading](https://www.sebi.gov.in/legal/circulars/feb-2025/safer-participation-of-retail-investors-in-algorithmic-trading_91614.html)

## Expected Outcomes
*   The repository documentation will serve as a complete operating manual, inclusive of the new Slack UI capabilities.
*   The project clearly signals its maturity by formally documenting structural adherence to Indian market regulations.
