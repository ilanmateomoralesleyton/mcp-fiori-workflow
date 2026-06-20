"""Entry point: python -m mcp_fiori_workflow"""
import asyncio
from mcp_fiori_workflow.server import main

if __name__ == "__main__":
    asyncio.run(main())
